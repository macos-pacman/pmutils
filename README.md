# pmutils

Scripts to maintain repositories under the `macos-pacman` project.

This script manages Pacman's `.db` file, wrapping over `repo-add`. More importantly, it automatically
uploads the updated database to GitHub releases, and also uploads package binaries as container images
for download by Pacman (requires patches).

## Configuration

Configuration is done by a toml file:

```toml
[registry]
url = "https://ghcr.io"
token = "ghp_YOURTOKENHERE"

[repository.core]
remote = "macos-pacman/core"
database = "/pm/db/core.db"
release-name = "pkg-db-arm64"
```

