# apworlds

Put the `.apworld` files used to generate the **registered seed** here (the same
`custom_worlds` you generate with). On the server, FTP/copy them into this folder.

They're read by `/register_seed` when it provisions the go-mode analyzer. In Docker this
folder is already inside the container at `/app/apworlds` via the `.:/app` mount, and
`GOMODE_APWORLDS_DIR=/app/apworlds` (set in `docker-compose.yml`) points the analyzer at it.

These are large, host-provided binaries and are **not** committed — only this README is
tracked (see `.gitignore`).
