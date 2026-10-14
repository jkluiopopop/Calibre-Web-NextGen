### Fixed

- **Forks can build their own dependency mirrors.** Dev and release image builds now publish Python and kepubify mirrors under the current repository owner's GHCR namespace and pass that same image reference to Dockerfile consumers, instead of trying to write to the upstream owner's package. Credential-free local builds still use public upstream downloads by default.

- Same-repository PRs in forks now use the internal build gates. Test builds prepare their own dependency cache with a separate package-write job, E2E resolves the current repository image, and registry authentication/network errors stop before rebuilding. Fast-test failures use the job summary; fork CI fetches genuine upstream release tags for changelog validation.
