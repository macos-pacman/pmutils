# pmutils

Scripts to maintain repositories under the `macos-pacman` project.

This script manages Pacman's `.db` file, wrapping over `repo-add`. More importantly, it automatically
uploads the updated database to GitHub releases, and also uploads package binaries as container images
for download by Pacman (requires patches).

## Configuration

Configuration is done by a toml file, which should be in `$HOME/.config/pmutils/config.toml`

```toml
[upstream]
url = "https://geo.mirror.pkgbuild.com/"

[registry]
url = "https://ghcr.io"
token = "ghp_YOURTOKENHERE"

[repository.core]
remote = "macos-pacman/core"
database = "/pm/db/core.db"
release-name = "pkg-db-arm64"

[checker]
check-out-of-date = false                   # whether to check if packages were flagged
											# "out of date" upstream

ignore-packages = ["libelf"]                # packages to ignore
ignore-package-epochs = ["go", "ffmpeg"]    # ignore epoch numbers for these packages

ignore-pkgrel = false                       # ignore pkgrel increments in general
ignore-haskell-pkgrel = true                # ignore pkgrel for haskell packages
```

