from . import semver

# This is used to detect if the user has already upgraded to a version of Rust
# Enhanced that implements plugin_unloaded() to handle upgrades.  See
# `cargo_build.plugin_unloaded` for more.
UPGRADE_SENTINEL = 1
