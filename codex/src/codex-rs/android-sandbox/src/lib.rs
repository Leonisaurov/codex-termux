//! `codex-linux-sandbox` para Android/Termux: traduce el `PermissionProfile` de Codex
//! a un arranque de `proot`.
//!
//! CODEX-TERMUX-ANDROID-PATCH: por qué existe este crate y por qué proot.
//!
//! Termux no tiene ninguna de las tres primitivas con las que upstream construye su
//! sandbox de Linux: Landlock exige kernel >= 5.13 (aquí es 5.10-android12 y la
//! syscall 444 muere por seccomp), `unshare(CLONE_NEWUSER|CLONE_NEWNS)` devuelve
//! EINVAL así que bubblewrap no puede montar ninguna vista de filesystem, y `bwrap`
//! no está instalado. `proot` sí existe y resuelve rutas por ptrace dentro del
//! proceso del usuario, que es el único mecanismo disponible en el dispositivo.
//!
//! Primitivas empleadas, medidas en el dispositivo:
//! - `-b /:/:ro` vuelve el filesystem completo no escribible: los `openat` con
//!   intención de escritura fallan con `EROFS` dentro del sandbox.
//! - un segundo `-b <raíz>:<raíz>` reabre la escritura en un subárbol, y
//!   `-b <sub>:<sub>:ro` la vuelve a cerrar dentro de esa raíz. Sobre una ruta que
//!   sí se puede leer, ese self-bind es el carveout correcto: bloquea escribir,
//!   borrar y renombrar sin ocultar el contenido.
//! - `-b <vacío>:<objetivo>:ro` oculta el contenido real e impide escribir, crear,
//!   borrar y renombrar la hoja; solo el nombre sigue visible en `ls`. Es la máscara
//!   que llevan las negaciones de lectura y los carveouts que aún no existen.
//! - `--net-policy deny` falla `connect`/`bind` con `EACCES`.
//!
//! Esto no es una frontera contra código deliberadamente malicioso: proot es ptrace
//! y el trazeé comparte UID con el trazador, así que un proceso que sepa buscar el
//! proceso de proot puede escapar. Es una frontera real contra escrituras y lecturas
//! accidentales, que es lo que `sandbox_mode` promete en un dispositivo sin root.
//!
//! Todo fallo de preparación (sin `proot`, sin capacidades, perfil no expresable)
//! termina en error con salida no cero: el wrapper nunca ejecuta el comando sin
//! sandbox ni degrada a permisos más amplios.

use std::ffi::OsString;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use codex_protocol::models::PermissionProfile;
use codex_protocol::protocol::FileSystemAccessMode;
use codex_protocol::protocol::FileSystemPath;
use codex_protocol::protocol::FileSystemSandboxKind;
use codex_protocol::protocol::FileSystemSandboxPolicy;
use codex_protocol::protocol::FileSystemSpecialPath;

/// Variable que permite señalar un `proot` concreto sin tocar el `PATH`.
pub const PROOT_ENV: &str = "CODEX_ANDROID_PROOT";
/// Si está definida, el wrapper anexa una línea con el argv recibido y el argv de
/// proot calculado. La usan los harnesses de `codex/test/sandbox-proot`.
pub const LOG_ENV: &str = "CODEX_ANDROID_SANDBOX_LOG";
/// Inspección del puerto: imprime el argv calculado y no ejecuta nada.
pub const DRY_RUN_FLAG: &str = "--dry-run";

/// EX_CONFIG: coincide con el código del stub que este wrapper reemplazó.
pub const SETUP_FAILURE_EXIT_CODE: i32 = 78;

/// Nodos que upstream crea frescos con `--dev /dev`. Aquí se reabren uno a uno sobre
/// un `/dev` read-only: el `/dev` real de Android no es ni legible para la app.
const WRITABLE_DEVICE_NODES: [&str; 6] = [
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
];

/// Soporte de máscaras, compartido entre invocaciones: solo nodos vacíos y todos los
/// binds que los usan son read-only, así que no hay nada que limpiar.
const MASK_DIR_NAME: &str = "codex-android-sandbox-mask";

#[derive(Debug, PartialEq, Eq)]
pub struct Request {
    pub sandbox_policy_cwd: PathBuf,
    pub command_cwd: Option<PathBuf>,
    pub permission_profile_json: String,
    pub use_legacy_landlock: bool,
    pub allow_network_for_proxy: bool,
    pub dry_run: bool,
    pub command: Vec<OsString>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub source: PathBuf,
    pub target: PathBuf,
    pub read_only: bool,
}

impl Binding {
    fn binding_spec(&self) -> OsString {
        let mut spec = OsString::from(self.source.as_os_str());
        spec.push(":");
        spec.push(self.target.as_os_str());
        if self.read_only {
            spec.push(":ro");
        }
        spec
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Plan {
    /// El perfil no pide nada que este wrapper pueda imponer: ejecutar directamente.
    Unsandboxed,
    Proot {
        deny_network: bool,
        bindings: Vec<Binding>,
    },
}

/// argv que se le pasaría a `proot`, sin contar el propio `proot`.
pub fn proot_argv(
    deny_network: bool,
    bindings: &[Binding],
    command: &[OsString],
) -> Vec<OsString> {
    let mut argv = Vec::new();
    if deny_network {
        argv.push(OsString::from("--net-policy"));
        argv.push(OsString::from("deny"));
    }
    for binding in bindings {
        argv.push(OsString::from("-b"));
        argv.push(binding.binding_spec());
    }
    // `proot` rechaza `--` como separador, así que el comando va suelto al final.
    argv.extend(command.iter().cloned());
    argv
}

/// Parsea el contrato que produce
/// `codex_sandboxing::landlock::create_linux_sandbox_command_args_for_permission_profile`.
/// Cualquier flag desconocido es error: si un re-vendor empieza a pedir algo que este
/// wrapper no impone, tiene que fallar en vez de ejecutar sin esa restricción.
pub fn parse_request(argv: &[OsString]) -> Result<Request, String> {
    let mut request = Request {
        sandbox_policy_cwd: PathBuf::new(),
        command_cwd: None,
        permission_profile_json: String::new(),
        use_legacy_landlock: false,
        allow_network_for_proxy: false,
        dry_run: false,
        command: Vec::new(),
    };
    let mut index = 0usize;
    let mut saw_separator = false;
    let mut saw_policy_cwd = false;
    let mut saw_profile = false;

    while index < argv.len() {
        let arg = &argv[index];
        if saw_separator {
            request.command.push(arg.clone());
            index += 1;
            continue;
        }
        if arg == "--" {
            saw_separator = true;
            index += 1;
            continue;
        }
        let read_value = |flag: &OsString| -> Result<OsString, String> {
            argv.get(index + 1).cloned().ok_or_else(|| {
                format!("{} requiere un valor", flag.to_string_lossy())
            })
        };
        if arg == "--sandbox-policy-cwd" {
            request.sandbox_policy_cwd = PathBuf::from(read_value(arg)?);
            saw_policy_cwd = true;
            index += 1;
        } else if arg == "--command-cwd" {
            request.command_cwd = Some(PathBuf::from(read_value(arg)?));
            index += 1;
        } else if arg == "--permission-profile" {
            request.permission_profile_json = read_value(arg)?.to_string_lossy().into_owned();
            saw_profile = true;
            index += 1;
        } else if arg == "--use-legacy-landlock" {
            request.use_legacy_landlock = true;
        } else if arg == "--allow-network-for-proxy" {
            request.allow_network_for_proxy = true;
        } else if arg == DRY_RUN_FLAG {
            request.dry_run = true;
        } else {
            return Err(format!(
                "argumento inesperado antes del comando: {}",
                arg.to_string_lossy()
            ));
        }
        index += 1;
    }

    if !saw_policy_cwd {
        return Err("falta --sandbox-policy-cwd".to_string());
    }
    if !saw_profile {
        return Err("falta --permission-profile".to_string());
    }
    if !saw_separator {
        return Err("falta el separador `--` antes del comando".to_string());
    }
    if request.command.is_empty() {
        return Err("el comando envuelto está vacío".to_string());
    }
    Ok(request)
}
/// Enlaces que Codex ya no puede tocar: la raíz read-only, los nodos de dispositivo
/// mínimos, las raíces escribibles del perfil y sus carveouts.
pub fn build_plan(profile: &PermissionProfile, cwd: &Path, mask_root: &Path) -> Result<Plan, String> {
    let file_system = profile.file_system_sandbox_policy();
    let network_enabled = profile.network_sandbox_policy().is_enabled();

    // External: un llamador externo ya impone el filesystem; de la red sigue
    // encargándose Codex, así que si la pide restringida se mantiene el bloqueo.
    if matches!(file_system.kind, FileSystemSandboxKind::ExternalSandbox) {
        return if network_enabled {
            Ok(Plan::Unsandboxed)
        } else {
            Ok(Plan::Proot {
                deny_network: true,
                bindings: Vec::new(),
            })
        };
    }
    let full_write = file_system.has_full_disk_write_access();
    if full_write && file_system.has_full_disk_read_access() && network_enabled {
        return Ok(Plan::Unsandboxed);
    }
    if !reads_are_root_wide(cwd, &file_system) {
        // Un perfil con allowlist de lecturas se expresa en bwrap con `--tmpfs /` más
        // binds selectivos. proot solo puede construir esa vista con `-r`, y eso
        // exigiría rehacer /proc, /dev, $PREFIX y los symlinks de Termux. Mejor fallar
        // que ejecutar con lecturas más amplias de las pedidas.
        return Err(
            "el perfil restringe qué se puede leer y el wrapper de proot no puede expresar \
             eso; usa sandbox_mode=danger-full-access o quita las entradas de lectura \
             restringida".to_string(),
        );
    }
    for entry in &file_system.entries {
        // Un glob de negación no es un bind: enmascarar el directorio padre ocultaría de
        // más, y no enmascarar nada ampliaría permisos, así que se falla cerrado.
        if entry.access == FileSystemAccessMode::Deny
            && matches!(
                &entry.path,
                FileSystemPath::GlobPattern { .. }
                    | FileSystemPath::Special {
                        value: FileSystemSpecialPath::Unknown { .. }
                    }
            )
        {
            return Err(format!(
                "el perfil niega la lectura con {} y el wrapper de proot solo sabe enmascarar \
                 rutas concretas",
                describe_denial(&entry.path)
            ));
        }
    }

    let mut bindings = Vec::new();
    if !full_write {
        bindings.push(Binding {
            source: PathBuf::from("/"),
            target: PathBuf::from("/"),
            read_only: true,
        });
        bindings.push(Binding {
            source: PathBuf::from("/dev"),
            target: PathBuf::from("/dev"),
            read_only: true,
        });
        for node in WRITABLE_DEVICE_NODES {
            let path = PathBuf::from(node);
            if path.exists() {
                bindings.push(Binding {
                    source: path.clone(),
                    target: path,
                    read_only: false,
                });
            }
        }

        let empty_file = mask_root.join("empty-file");
        let empty_dir = mask_root.join("empty-dir");
        for root in file_system.get_writable_roots_with_cwd(cwd) {
            let root_path = root.root.as_path();
            // Raíces que no existen en este dispositivo (p. ej. /tmp fuera de Termux)
            // se omiten, igual que hace el builder de bwrap.
            if root_path.exists() {
                bindings.push(Binding {
                    source: root_path.to_path_buf(),
                    target: root_path.to_path_buf(),
                    read_only: false,
                });
                for subpath in &root.read_only_subpaths {
                    bindings.push(carveout_binding(
                        subpath.as_path(),
                        cwd,
                        &file_system,
                        &empty_dir,
                        &empty_file,
                    ));
                }
            }
            // Un nombre protegido puede no existir todavía; sin máscara el comando
            // podría crearlo bajo una raíz escribible, así que se enlaza vacío.
            for name in &root.protected_metadata_names {
                bindings.push(carveout_binding(
                    &root_path.join(name),
                    cwd,
                    &file_system,
                    &empty_dir,
                    &empty_file,
                ));
            }
        }
        // Las negaciones de lectura se resuelven aquí y no con
        // `get_unreadable_roots_with_cwd`: con lectura de disco completa esa función
        // descarta toda entrada negada porque el propio camino la considera legible, y
        // el perfil seguiría negándola por entrada.
        for path in denied_read_paths(&file_system, cwd)? {
            bindings.push(Binding {
                source: source_for(&path, &empty_dir, &empty_file),
                target: path,
                read_only: true,
            });
        }
    }

    Ok(Plan::Proot {
        deny_network: !network_enabled,
        bindings: order_bindings(bindings),
    })
}

/// Bind de un carveout dentro de una raíz escribible.
///
/// Dos casos que upstream separa y que un self-bind confundiría:
/// - lectura permitida y ruta existente (`<ws>/.git`): se enlaza la ruta contra sí misma
///   en read-only, porque un bind de origen vacío ocultaría el contenido que sí se puede
///   leer. Medido: `:ro` sobre sí misma bloquea escribir, borrar y renombrar.
/// - lectura negada, o ruta que falta (`<ws>/.codex` aún no creado): se enlaza un nodo
///   vacío. Un bind con fuente inexistente aborta a proot; la máscara vacía sí bloquea
///   lectura, escritura, creación, borrado y renombrado de la hoja.
fn carveout_binding(
    path: &Path,
    cwd: &Path,
    file_system: &FileSystemSandboxPolicy,
    empty_dir: &Path,
    empty_file: &Path,
) -> Binding {
    let readable = path.exists() && file_system.can_read_local_path_with_cwd(path, cwd);
    Binding {
        source: if readable {
            path.to_path_buf()
        } else {
            source_for(path, empty_dir, empty_file)
        },
        target: path.to_path_buf(),
        read_only: true,
    }
}

/// Rutas concretas que el perfil niega leer, resueltas contra `cwd`.
///
/// `get_unreadable_roots_with_cwd` no basta: descarta toda negación cuando el perfil
/// también otorga lectura de disco completa, así que esas rutas quedarían legibles.
fn denied_read_paths(
    file_system: &FileSystemSandboxPolicy,
    cwd: &Path,
) -> Result<Vec<PathBuf>, String> {
    let mut paths = Vec::new();
    for entry in &file_system.entries {
        if entry.access != FileSystemAccessMode::Deny {
            continue;
        }
        match &entry.path {
            FileSystemPath::Path { path } => paths.push(path.to_path_buf()),
            FileSystemPath::GlobPattern { .. } => {}
            FileSystemPath::Special { value } => paths.push(match value {
                // Enmascarar `/` borraría el filesystem entero, incluido el programa a
                // ejecutar, y no enmascarar nada dejaría legible justo lo que el perfil
                // niega. Un perfil que otorga lectura de disco y la niega a la vez es
                // contradictorio y no tiene interpretación segura, así que se falla.
                FileSystemSpecialPath::Root => {
                    return Err(
                        "el perfil niega la lectura de todo el disco y el wrapper de proot no \
                         puede enmascarar la raíz sin borrar también el programa a ejecutar"
                            .to_string(),
                    );
                }
                FileSystemSpecialPath::SlashTmp => PathBuf::from("/tmp"),
                FileSystemSpecialPath::Tmpdir => std::env::temp_dir(),
                FileSystemSpecialPath::ProjectRoots { subpath } => match subpath {
                    Some(subpath) => cwd.join(subpath),
                    None => cwd.to_path_buf(),
                },
                // `Minimal` es un conjunto de rutas del runtime, no una ruta: negar lo
                // que no se puede resolver enmascararía otra cosa, así que se falla.
                FileSystemSpecialPath::Minimal | FileSystemSpecialPath::Unknown { .. } => {
                    return Err(format!(
                        "el perfil niega la lectura de una ruta especial no resoluble ({:?}) y \
                         el wrapper de proot no sabe qué enmascarar",
                        value
                    ));
                }
            }),
        }
    }
    Ok(paths)
}

/// Los binds de proot se resuelven en orden: el último que coincide gana. Se ordenan de
/// menos a más específico para que un carveout nunca quede tapado por su raíz escribible,
/// y a igual ruta gana la versión read-only para que un empate no ensanche permisos.
fn order_bindings(bindings: Vec<Binding>) -> Vec<Binding> {
    let mut bindings = bindings;
    bindings.sort_by_key(|binding| {
        (
            binding.target.components().count(),
            binding.target.clone(),
            binding.read_only,
        )
    });
    bindings.dedup_by(|a, b| a.target == b.target && a.read_only == b.read_only);
    bindings
}

fn source_for(path: &Path, empty_dir: &Path, empty_file: &Path) -> PathBuf {
    if path.is_dir() {
        empty_dir.to_path_buf()
    } else {
        empty_file.to_path_buf()
    }
}

/// `true` cuando todas las lecturas caben en un `/` legible con máscaras puntuales.
/// Con deny-entries presentes `has_full_disk_read_access()` es `false` aunque la raíz
/// sea legible, así que se comprueba además si `/` aparece entre las raíces explícitas.
fn reads_are_root_wide(cwd: &Path, file_system: &FileSystemSandboxPolicy) -> bool {
    file_system.has_full_disk_read_access()
        || file_system
            .get_readable_roots_with_cwd(cwd)
            .iter()
            .any(|path| path.as_path() == Path::new("/"))
}

fn describe_denial(path: &FileSystemPath) -> &'static str {
    match path {
        FileSystemPath::GlobPattern { .. } => "un patrón glob",
        _ => "una ruta especial no reconocida",
    }
}

fn prepare_mask_root() -> Result<PathBuf, String> {
    let root = std::env::temp_dir().join(MASK_DIR_NAME);
    fs::create_dir_all(&root).map_err(|e| format!("no se pudo crear {}: {e}", root.display()))?;
    fs::create_dir_all(root.join("empty-dir"))
        .map_err(|e| format!("no se pudo crear la máscara de directorio: {e}"))?;
    fs::File::create(root.join("empty-file"))
        .map_err(|e| format!("no se pudo crear la máscara de archivo: {e}"))?;
    Ok(root)
}

fn is_executable(path: &Path) -> bool {
    fs::metadata(path)
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

fn resolve_proot() -> Result<PathBuf, String> {
    if let Some(explicit) = std::env::var_os(PROOT_ENV) {
        let path = PathBuf::from(&explicit);
        return if is_executable(&path) {
            Ok(path)
        } else {
            Err(format!(
                "{PROOT_ENV} no apunta a un ejecutable: {}",
                path.display()
            ))
        };
    }
    if let Some(prefix) = std::env::var_os("PREFIX") {
        let candidate = PathBuf::from(prefix).join("bin").join("proot");
        if is_executable(&candidate) {
            return Ok(candidate);
        }
    }
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            let candidate = dir.join("proot");
            if is_executable(&candidate) {
                return Ok(candidate);
            }
        }
    }
    Err("proot no está instalado: `pkg install proot`, o pon sandbox_mode=danger-full-access \
         si asumes el riesgo"
        .to_string())
}

fn run_proot_probe(proot: &Path, args: &[OsString]) -> Result<bool, String> {
    let status = Command::new(proot)
        .args(args)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("no se pudo ejecutar {}: {e}", proot.display()))?;
    Ok(status.success())
}

/// Un proot puede aceptar la sintaxis `:ro` sin aplicarla, que es justo el fallo que
/// convertiría el sandbox en decorado. Se comprueba en tres pasos: que el bind se
/// acepte, que un `openat` con escritura dentro de él falle, y que una máscara sobre una
/// ruta inexistente impida crearla (los carveouts `.git`/`.codex` se enlazan aunque no
/// estén ahí todavía).
fn probe_read_only_bindings(proot: &Path, mask_root: &Path) -> Result<(), String> {
    let scratch = mask_root
        .join("probe")
        .join(std::process::id().to_string());
    fs::create_dir_all(&scratch)
        .map_err(|e| format!("no se pudo crear el directorio de sonda: {e}"))?;
    let mask = mask_root.join("empty-file");
    let spec = format!("{}:{}:ro", scratch.display(), scratch.display());
    let accepted = vec![
        OsString::from("-b"),
        OsString::from(&spec),
        OsString::from("sh"),
        OsString::from("-c"),
        OsString::from("exit 0"),
    ];
    if !run_proot_probe(proot, &accepted)? {
        return Err("proot no acepta bindings `-b origen:destino:ro`; se necesita un proot \
                    de Termux reciente"
            .to_string());
    }
    let write_attempt = vec![
        OsString::from("-b"),
        OsString::from(spec),
        OsString::from("sh"),
        OsString::from("-c"),
        OsString::from(format!("echo probe > {}/canary", scratch.display())),
    ];
    if run_proot_probe(proot, &write_attempt)? {
        return Err("proot acepta `:ro` pero no lo aplica: el sandbox no aislaría escrituras".to_string());
    }
    let missing = scratch.join("todavia-no-existe");
    let missing_attempt = vec![
        OsString::from("-b"),
        OsString::from(format!("{}:{}:ro", mask.display(), missing.display())),
        OsString::from("sh"),
        OsString::from("-c"),
        OsString::from(format!("echo probe > {}/canary", missing.display())),
    ];
    if run_proot_probe(proot, &missing_attempt)? {
        return Err("proot no aplica máscaras sobre rutas inexistentes: los carveouts de \
                    metadata se podrían crear igualmente"
            .to_string());
    }
    let _ = fs::remove_dir_all(&scratch);
    Ok(())
}

fn probe_network_policy(proot: &Path) -> Result<(), String> {
    let args = vec![
        OsString::from("--net-policy"),
        OsString::from("deny"),
        OsString::from("sh"),
        OsString::from("-c"),
        OsString::from("exit 0"),
    ];
    if run_proot_probe(proot, &args)? {
        Ok(())
    } else {
        Err("el perfil pide red bloqueada pero este proot no soporta --net-policy; ejecutar \
             el comando sin esa restricción sería ampliar los permisos"
            .to_string())
    }
}

fn log_record(argv: &[OsString], emitted: &[OsString]) {
    let Some(path) = std::env::var_os(LOG_ENV) else {
        return;
    };
    let record = serde_json::json!({
        "received_argv": argv.iter().map(|a| a.to_string_lossy().into_owned()).collect::<Vec<_>>(),
        "proot_argv": emitted.iter().map(|a| a.to_string_lossy().into_owned()).collect::<Vec<_>>(),
    });
    if let Ok(line) = serde_json::to_string(&record) {
        use std::io::Write;
        if let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(file, "{line}");
        }
    }
}

/// Punto de entrada del binario: nunca devuelve normalmente, salvo en `--dry-run`.
pub fn run_main() -> ! {
    let argv: Vec<OsString> = std::env::args_os().skip(1).collect();
    match run(&argv) {
        // El camino real termina en exec(2) y no vuelve; Ok solo llega del dry-run.
        Ok(()) => std::process::exit(0),
        Err(message) => {
            eprintln!("codex-linux-sandbox(android): {message}");
            std::process::exit(SETUP_FAILURE_EXIT_CODE);
        }
    }
}

fn run(argv: &[OsString]) -> Result<(), String> {
    let request = parse_request(argv)?;
    let profile: PermissionProfile = serde_json::from_str(&request.permission_profile_json)
        .map_err(|e| format!("perfil de permisos inválido: {e}"))?;

    if request.use_legacy_landlock {
        eprintln!(
            "codex-linux-sandbox(android): --use-legacy-landlock se ignora; en Android las \
             restricciones las impone proot en todos los casos"
        );
    }
    if request.allow_network_for_proxy && !profile.network_sandbox_policy().is_enabled() {
        // La red gestionada exige enrutar solo hacia el proxy; el wrapper no recibe la
        // ruta del proxy en el argv, así que no puede expresarlo.
        return Err("la red gestionada (--allow-network-for-proxy) no está soportada por el \
                    wrapper de proot; desactiva enforce_managed_network en Termux"
            .to_string());
    }

    let mask_root = prepare_mask_root()?;
    let plan = build_plan(&profile, &request.sandbox_policy_cwd, &mask_root)?;

    if request.dry_run {
        let emitted = match &plan {
            Plan::Unsandboxed => request.command.clone(),
            Plan::Proot {
                deny_network,
                bindings,
            } => proot_argv(*deny_network, bindings, &request.command),
        };
        for arg in &emitted {
            println!("{}", arg.to_string_lossy());
        }
        return Ok(());
    }

    let (proot_path, emitted) = match &plan {
        Plan::Unsandboxed => (None, request.command.clone()),
        Plan::Proot {
            deny_network,
            bindings,
        } => {
            let proot = resolve_proot()?;
            if bindings.iter().any(|binding| binding.read_only) {
                probe_read_only_bindings(&proot, &mask_root)?;
            }
            if *deny_network {
                probe_network_policy(&proot)?;
            }
            (Some(proot), proot_argv(*deny_network, bindings, &request.command))
        }
    };
    log_record(argv, &emitted);

    let mut command = match proot_path {
        Some(proot) => {
            let mut command = Command::new(&proot);
            command.args(&emitted);
            command
        }
        None => {
            let mut command = Command::new(&request.command[0]);
            command.args(&request.command[1..]);
            command
        }
    };
    if let Some(cwd) = request.command_cwd.filter(|cwd| cwd.is_dir()) {
        command.current_dir(cwd);
    }
    let error = command.exec();
    Err(format!("no se pudo ejecutar el comando envuelto: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_protocol::protocol::NetworkSandboxPolicy;

    fn os_argv(argv: &[&str]) -> Vec<OsString> {
        argv.iter().map(OsString::from).collect()
    }

    /// argv tal como lo arma `create_linux_sandbox_command_args_for_permission_profile`.
    fn manager_argv(cwd: &Path, profile_json: &str) -> Vec<OsString> {
        let mut argv = vec![
            OsString::from("--sandbox-policy-cwd"),
            cwd.as_os_str().to_os_string(),
            OsString::from("--permission-profile"),
            OsString::from(profile_json),
        ];
        argv.extend(os_argv(&["--", "/system/bin/sh", "-c", "exit 42"]));
        argv
    }

    fn plan_for(profile: &PermissionProfile, cwd: &Path) -> Plan {
        build_plan(profile, cwd, Path::new("/tmp/mask-root")).expect("plan expresable")
    }

    fn bindings_of<'a>(plan: &'a Plan, target: &Path) -> Vec<&'a Binding> {
        match plan {
            Plan::Unsandboxed => Vec::new(),
            Plan::Proot { bindings, .. } => bindings
                .iter()
                .filter(|binding| binding.target == target)
                .collect(),
        }
    }

    fn position(plan: &Plan, target: &Path, read_only: bool) -> usize {
        match plan {
            Plan::Unsandboxed => panic!("el plan no es de proot"),
            Plan::Proot { bindings, .. } => bindings
                .iter()
                .position(|binding| {
                    binding.target == target && binding.read_only == read_only
                })
                .unwrap_or_else(|| panic!("falta el bind {} (ro={read_only})", target.display())),
        }
    }

    #[test]
    fn parses_the_manager_contract() {
        let profile = serde_json::to_string(&PermissionProfile::read_only()).unwrap();
        let argv = manager_argv(Path::new("/ws"), &profile);
        let request = parse_request(&argv).unwrap();

        assert_eq!(request.sandbox_policy_cwd, PathBuf::from("/ws"));
        assert_eq!(request.command_cwd, None);
        assert!(!request.use_legacy_landlock);
        assert!(!request.dry_run);
        assert_eq!(request.command, os_argv(&["/system/bin/sh", "-c", "exit 42"]));
        assert_eq!(request.permission_profile_json, profile);
    }

    #[test]
    fn unexpected_arguments_fail_closed() {
        let profile = serde_json::to_string(&PermissionProfile::read_only()).unwrap();
        let mut argv = manager_argv(Path::new("/ws"), &profile);
        argv.insert(2, OsString::from("--new-sandbox-knob"));
        assert!(parse_request(&argv).is_err());
    }

    #[test]
    fn a_command_after_the_separator_is_mandatory() {
        let profile = serde_json::to_string(&PermissionProfile::read_only()).unwrap();
        let argv = os_argv(&[
            "--sandbox-policy-cwd",
            "/ws",
            "--permission-profile",
            &profile,
        ]);
        assert!(parse_request(&argv).is_err());
    }

    #[test]
    fn proot_receives_no_double_dash() {
        let bindings = vec![Binding {
            source: PathBuf::from("/"),
            target: PathBuf::from("/"),
            read_only: true,
        }];
        let argv = proot_argv(true, &bindings, &os_argv(&["sh", "-c", "exit 42"]));

        assert!(!argv.contains(&OsString::from("--")));
        assert_eq!(argv[0], OsString::from("--net-policy"));
        assert_eq!(argv[1], OsString::from("deny"));
        assert_eq!(argv[2], OsString::from("-b"));
        assert_eq!(argv[3], OsString::from("/:/:ro"));
        assert_eq!(argv[4..], os_argv(&["sh", "-c", "exit 42"])[..]);
    }

    #[test]
    fn workspace_write_masks_the_rest_of_the_disk() {
        let workspace = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(workspace.path().join(".git")).unwrap();
        std::fs::write(workspace.path().join(".git/config"), b"[core]\n").unwrap();

        let plan = plan_for(&PermissionProfile::workspace_write(), workspace.path());
        let Plan::Proot {
            deny_network,
            bindings,
        } = &plan
        else {
            panic!("workspace-write debe envolver con proot");
        };

        assert!(deny_network, "workspace-write pide red restringida");
        let root_binds = bindings_of(&plan, Path::new("/"));
        assert_eq!(root_binds.len(), 1);
        assert!(root_binds[0].read_only);
        assert!(bindings_of(&plan, workspace.path())
            .iter()
            .any(|binding| !binding.read_only));
        // `.git` se enlaza contra sí misma: existe y su lectura está permitida, así que
        // una máscara la ocultaría y el sandbox sería más estricto que el pedido.
        let git = bindings_of(&plan, &workspace.path().join(".git"));
        assert_eq!(git.len(), 1);
        assert!(git[0].read_only);
        assert_eq!(git[0].source, workspace.path().join(".git"));
        // `.codex` todavía no existe y aun así se enmascara: sin bind, el comando
        // podría crearlo y saltarse la aprobación de metadata.
        let masked_codex = bindings_of(&plan, &workspace.path().join(".codex"));
        assert_eq!(masked_codex.len(), 1);
        assert!(masked_codex[0].read_only);
        assert!(masked_codex[0].source.starts_with("/tmp/mask-root"));

        // El orden es semántico en proot: gana el último bind que coincide.
        assert!(position(&plan, Path::new("/"), true) < position(&plan, workspace.path(), false));
        assert!(position(&plan, workspace.path(), false) < position(&plan, &workspace.path().join(".git"), true));
    }

    #[test]
    fn read_only_profile_leaves_nothing_writable_but_the_device_nodes() {
        let workspace = tempfile::tempdir().unwrap();
        let plan = plan_for(&PermissionProfile::read_only(), workspace.path());
        let Plan::Proot {
            deny_network,
            bindings,
        } = &plan
        else {
            panic!("read-only debe envolver con proot");
        };

        assert!(deny_network);
        assert!(bindings.iter().all(|binding| binding.read_only
            || WRITABLE_DEVICE_NODES
                .iter()
                .any(|node| Path::new(node) == binding.target)));
        assert!(bindings_of(&plan, workspace.path()).is_empty());
    }

    #[test]
    fn danger_full_access_runs_unsandboxed() {
        let workspace = tempfile::tempdir().unwrap();
        assert_eq!(
            plan_for(&PermissionProfile::Disabled, workspace.path()),
            Plan::Unsandboxed
        );
    }

    #[test]
    fn external_profile_still_denies_network_when_asked() {
        let workspace = tempfile::tempdir().unwrap();
        let restricted = PermissionProfile::External {
            network: NetworkSandboxPolicy::Restricted,
        };
        match plan_for(&restricted, workspace.path()) {
            Plan::Proot {
                deny_network,
                bindings,
            } => {
                assert!(deny_network);
                assert!(bindings.is_empty(), "el filesystem lo impone el llamador externo");
            }
            Plan::Unsandboxed => panic!("la red pedida como restringida no puede ignorarse"),
        }
        let open = PermissionProfile::External {
            network: NetworkSandboxPolicy::Enabled,
        };
        assert_eq!(plan_for(&open, workspace.path()), Plan::Unsandboxed);
    }

    #[test]
    fn a_read_allowlist_profile_fails_closed() {
        let workspace = tempfile::tempdir().unwrap();
        // Sin la entrada Root/read, expresar esto exigiría reconstruir la raíz completa.
        let json = format!(
            r#"{{"type":"managed","file_system":{{"type":"restricted","entries":[
                {{"path":{{"type":"path","path":"{}"}},"access":"write"}}
            ]}},"network":"enabled"}}"#,
            workspace.path().display()
        );
        let profile: PermissionProfile = serde_json::from_str(&json).unwrap();

        let error = build_plan(&profile, workspace.path(), Path::new("/tmp/mask-root"))
            .expect_err("las lecturas restringidas no son expresables");
        assert!(error.contains("no puede expresar"), "{error}");
    }

    #[test]
    fn a_glob_denial_fails_closed_even_with_root_read() {
        let workspace = tempfile::tempdir().unwrap();
        let json = r#"{"type":"managed","file_system":{"type":"restricted","entries":[
            {"path":{"type":"special","value":{"kind":"root"}},"access":"read"},
            {"path":{"type":"glob_pattern","pattern":"**/*.pem"},"access":"deny"}
        ]},"network":"enabled"}"#;
        let profile: PermissionProfile = serde_json::from_str(json).unwrap();

        let error = build_plan(&profile, workspace.path(), Path::new("/tmp/mask-root"))
            .expect_err("un glob de negación no es un bind");
        assert!(error.contains("glob"), "{error}");
    }

    #[test]
    fn a_concrete_denial_is_masked_not_rejected() {
        let workspace = tempfile::tempdir().unwrap();
        let secret = workspace.path().join("secret");
        std::fs::create_dir_all(&secret).unwrap();
        let json = format!(
            r#"{{"type":"managed","file_system":{{"type":"restricted","entries":[
                {{"path":{{"type":"special","value":{{"kind":"root"}}}},"access":"read"}},
                {{"path":{{"type":"path","path":"{}"}},"access":"write"}},
                {{"path":{{"type":"path","path":"{}"}},"access":"deny"}}
            ]}},"network":"enabled"}}"#,
            workspace.path().display(),
            secret.display()
        );
        let profile: PermissionProfile = serde_json::from_str(&json).unwrap();

        let plan = plan_for(&profile, workspace.path());
        let masked = bindings_of(&plan, &secret);
        assert_eq!(masked.len(), 1);
        assert!(masked[0].read_only);
        assert!(masked[0].source.starts_with("/tmp/mask-root"));
    }

    #[test]
    fn a_denial_outside_every_writable_root_is_masked_too() {
        // Con lectura de disco completa `get_unreadable_roots_with_cwd` descarta la
        // entrada negada porque el propio camino la considera legible; si el wrapper se
        // guiara solo por esa función, la ruta quedaría legible y el sandbox sería más
        // amplio que lo pedido.
        let workspace = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let secret = outside.path().join("claves");
        std::fs::create_dir_all(&secret).unwrap();
        let json = format!(
            r#"{{"type":"managed","file_system":{{"type":"restricted","entries":[
                {{"path":{{"type":"special","value":{{"kind":"root"}}}},"access":"read"}},
                {{"path":{{"type":"path","path":"{}"}},"access":"write"}},
                {{"path":{{"type":"path","path":"{}"}},"access":"deny"}}
            ]}},"network":"enabled"}}"#,
            workspace.path().display(),
            secret.display()
        );
        let profile: PermissionProfile = serde_json::from_str(&json).unwrap();

        let plan = plan_for(&profile, workspace.path());
        let masked = bindings_of(&plan, &secret);
        assert_eq!(masked.len(), 1);
        assert!(masked[0].read_only);
        assert!(
            masked[0].source.starts_with("/tmp/mask-root"),
            "una negación de lectura existente se enmascara, no se autoenlaza: {:?}",
            masked[0].source
        );
    }

    #[test]
    fn an_unresolvable_denial_fails_closed() {
        let workspace = tempfile::tempdir().unwrap();
        let json = r#"{"type":"managed","file_system":{"type":"restricted","entries":[
            {"path":{"type":"special","value":{"kind":"root"}},"access":"read"},
            {"path":{"type":"special","value":{"kind":"minimal"}},"access":"deny"}
        ]},"network":"enabled"}"#;
        let profile: PermissionProfile = serde_json::from_str(json).unwrap();

        let error = build_plan(&profile, workspace.path(), Path::new("/tmp/mask-root"))
            .expect_err("una negación sin ruta concreta no se puede enmascarar");
        assert!(error.contains("no resoluble"), "{error}");
    }

    #[test]
    fn a_root_read_denial_fails_closed_rather_than_masking_the_root() {
        // Negar la lectura de `/` no tiene traducción a binds: enmascararla quitaría
        // también el programa a ejecutar y no enmascararla dejaría legible todo lo
        // negado. Puede detectarlo la puerta de allowlist (sin lectura raíz no hay
        // plan posible) o la resolución de la negación; ambas fallan cerrado.
        let workspace = tempfile::tempdir().unwrap();
        let json = r#"{"type":"managed","file_system":{"type":"restricted","entries":[
            {"path":{"type":"special","value":{"kind":"root"}},"access":"read"},
            {"path":{"type":"special","value":{"kind":"project_roots"}},"access":"write"},
            {"path":{"type":"special","value":{"kind":"root"}},"access":"deny"}
        ]},"network":"enabled"}"#;
        let profile: PermissionProfile = serde_json::from_str(json).unwrap();

        let error = build_plan(&profile, workspace.path(), Path::new("/tmp/mask-root"))
            .expect_err("la negación de la raíz no se expresa con binds");
        assert!(
            error.contains("restringe qué se puede leer")
                || error.contains("no puede enmascarar la raíz"),
            "{error}"
        );
    }

    #[test]
    fn the_wire_shape_of_the_profile_is_pinned() {
        // Este es el contrato exacto que `run()` parsea: si upstream renombrara un
        // tag, el wrapper dejaría de entender el perfil y tiene que fallar aquí.
        let json = serde_json::to_string(&PermissionProfile::read_only()).unwrap();
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(value["type"], "managed");
        assert_eq!(value["network"], "restricted");
        assert_eq!(value["file_system"]["type"], "restricted");
        assert_eq!(value["file_system"]["entries"][0]["path"]["type"], "special");
        assert_eq!(value["file_system"]["entries"][0]["path"]["value"]["kind"], "root");
        assert_eq!(value["file_system"]["entries"][0]["access"], "read");
        assert_eq!(serde_json::from_str::<PermissionProfile>(&json).unwrap(), PermissionProfile::read_only());
    }
}
