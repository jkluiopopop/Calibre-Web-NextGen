### Fixed

- **Forks can build their own dependency mirrors.** Dev and release image builds now publish Python and kepubify mirrors under the current repository owner's GHCR namespace and pass that same image reference to Dockerfile consumers, instead of trying to write to the upstream owner's package. Credential-free local builds still use public upstream downloads by default.
