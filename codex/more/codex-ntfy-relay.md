# Relay de aprobaciones Codex → ntfy

`codex-ntfy-relay.ts` es un cliente-relay JSONL para `codex app-server`. Se
coloca entre un cliente de app-server y `codex-android app-server`; solo
intercepta:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`

Todo lo demás atraviesa el relay sin transformación. Las notificaciones ntfy
solo muestran `Aceptar`, `Denegar` y `⋯`. El tercer botón abre un panel vivo
generado para esa ejecución del relay. El panel se actualiza automáticamente y
permite:

- aceptar o denegar solicitudes pendientes;
- denegar y enviar un motivo mediante `turn/steer`;
- enviar un mensaje adicional al turno activo mediante el campo de motivo;
- permitir la sesión completa;
- guardar un prefijo exacto del comando para autoaceptarlo durante esta sesión;
- **permitir siempre**: manda la enmienda propuesta por el app-server
  (`acceptWithExecpolicyAmendment`), que Codex escribe como regla `allow` en
  `<CODEX_HOME>/rules/default.rules`.

El prefijo de sesión se conserva únicamente en memoria del relay. La acción
"Permitir siempre (regla)" sí persiste, y solo está disponible cuando el
app-server propone una enmienda (en la TUI equivale a "Yes, and don't ask again
for commands that start with …"). La enmienda se reenvía tal cual la propuso el
app-server: acortarla permitiría comandos que el usuario nunca aprobó.

Esa escritura necesita un binario con el parche Android de `codex-execpolicy`
(el port lo trae desde el vendoring; `File::lock` devuelve
`ErrorKind::Unsupported` en `aarch64-linux-android` con el toolchain fijado). Con
un binario sin el parche el app-server responde con un warning y la aprobación
se aplica solo a esa ejecución. Para volver al comportamiento estrictamente en
memoria, define `CODEX_NTFY_PERSIST_RULES=0`.

## Ejecución

El relay exige autenticación en ambos sentidos:

```sh
export CODEX_NTFY_PUBLISH_TOKEN='token-de-publicacion'
export CODEX_NTFY_HOOK_TOKEN='token-del-webhook'
export NTFY_URL='http://localhost:8086'
export NTFY_TOPIC='codex'
bun run scripts/codex-ntfy-relay.ts
```

El relay detecta automáticamente la IP LAN mediante `ip route` o
`hostname -I` y la usa para construir la URL de los botones y del panel. El
servidor escucha en `0.0.0.0`. Si la detección falla, define manualmente
`CODEX_NTFY_CALLBACK_HOST` con la IP que ve el teléfono. No se coloca ningún
token de ntfy en una URL.

El topic predeterminado es `codex`; el teléfono debe suscribirse a ese topic
en el mismo servidor ntfy. Se puede cambiar con `NTFY_TOPIC`. No se crea un
usuario nuevo: el token de publicación se toma de `CODEX_NTFY_PUBLISH_TOKEN`
o, como compatibilidad, de `NTFY_OPENCODE_TOKEN`/`NTFY_TOKEN`. El relay sí
requiere un `CODEX_NTFY_HOOK_TOKEN` separado para autenticar callbacks.

Variables opcionales:

- `CODEX_NTFY_CODEX_BIN` (por defecto `codex-android`)
- `CODEX_NTFY_CODEX_ARGS` (por defecto `app-server --stdio`)
- `CODEX_NTFY_HOOK_PORT` (por defecto `10009`)
- `CODEX_NTFY_PERSIST_RULES` (por defecto `1`; `0` deshabilita “Permitir siempre
  (regla)” y deja el relay estrictamente en memoria)
- `CODEX_NTFY_APPROVAL_TTL_MS` (por defecto `0`, sin expiración corta; el cierre
  del proceso siempre invalida las solicitudes)

El proceso debe ser usado como transporte del cliente de app-server. No
reemplaza la TUI de `codex-android` ni convierte automáticamente una sesión
interactiva existente en app-server.
