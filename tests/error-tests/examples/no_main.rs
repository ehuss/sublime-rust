// Should display error about no main.

mod no_main_mod;
// Not sure why no-trans doesn't handle this properly.
// When --profile=test is used with `cargo check`, this error will not happen
// due to the synthesized main created by the test harness.
// end-msg: ERR(rust_syntax_checking_include_tests=False) main function not found
// end-msg: NOTE(rust_syntax_checking_include_tests=False) the main function must be defined
// end-msg: MSG(rust_syntax_checking_include_tests=False) See Also: no_main_mod.rs:1
