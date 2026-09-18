# Signed Firefox builds

Drop the Mozilla-signed `.xpi` from the AMO Developer Hub here, named
`oga-extension-<version>.xpi`, then set `EXTENSION_XPI_VERSION` to that version
in Render.

`/extension/firefox.xpi` then serves it as `application/x-xpinstall`, which is
what makes Firefox offer to install it rather than download it, and
`/extension/updates.json` starts offering it to already-installed copies.

Take the file from the version's own page (Manage Status & Versions → the
version → Files), not from the submission you uploaded. Signed builds end
`.xpi` and contain a `META-INF/` directory; the upload copy is a `.zip` without
one, and Firefox refuses to install it. `unzip -l <file> | grep META-INF` before
committing.

These files are Mozilla-signed artefacts, not source. The source that produced
them is the repo itself; the zip that gets uploaded for signing is built by
`core/extension_index.py::build_zip_bytes` and downloaded from the **Release
row at the foot of the pipeline hub** — see `../README.md`'s Distribution
section for the full order, including the part that bites: deploy the site
first, because the packager reads the live database at the moment you press it.
