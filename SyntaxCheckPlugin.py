import sublime
import sublime_plugin
import os
from .rust import messages, rust_proc, rust_thread, util, target_detect
from pprint import pprint


"""On-save syntax checking.

This contains the code for displaying message phantoms for errors/warnings
whenever you save a Rust file.
"""

# Notes:
# - -Zno-trans produces a warning about being unstable (see
#   https://github.com/rust-lang/rust/issues/31847). I am uncertain about the
#   long-term prospects of how this will be resolved.  There are a few things
#   to consider:
#       - Cargo recently added "cargo check"
#         (https://github.com/rust-lang/cargo/pull/3296), which more or less
#         does the same thing.  See also the original "cargo check" addon
#         (https://github.com/rsolomo/cargo-check/).
#       - RLS was recently released
#         (https://github.com/rust-lang-nursery/rls).  It's unclear to me if
#         this will perform full-linting that could replace this or not.
#
# - -Zno-trans prevents some warnings and errors from being generated. For
#   example, see const-err.rs.  "cargo check" will solve this, but it is
#   nightly only right now. Other issues:
#       - Errors generated by compiling an extern crate do not not output as
#         json.


class RustSyntaxCheckEvent(sublime_plugin.EventListener):

    # Beware: This gets called multiple times if the same buffer is opened in
    # multiple views (with the same view passed in each time).  See:
    # https://github.com/SublimeTextIssues/Core/issues/289
    def on_post_save(self, view):
        # Are we in rust scope and is it switched on?
        # We use phantoms which were added in 3118
        if int(sublime.version()) < 3118:
            return

        enabled = util.get_setting('rust_syntax_checking', True)
        if enabled and util.active_view_is_rust(view=view):
            t = RustSyntaxCheckThread(view)
            t.start()
        elif not enabled:
            # If the user has switched OFF the plugin, remove any phantom
            # lines.
            messages.clear_messages(view.window())


class RustSyntaxCheckThread(rust_thread.RustThread, rust_proc.ProcListener):

    # Thread name.
    name = 'Syntax Check'
    # The Sublime view that triggered the check.
    view = None
    # Absolute path to the view that triggered the check.
    triggered_file_name = None
    # Directory of `triggered_file_name`.
    cwd = None
    # This flag is used to terminate early. In situations where we can't
    # auto-detect the appropriate Cargo target, we compile multiple targets.
    # If we receive any messages for the current view, we might as well stop.
    # Otherwise, you risk displaying duplicate messages for shared modules.
    this_view_found = False
    # The path to the top-level Cargo target filename (like main.rs or
    # lib.rs).
    current_target_src = None

    def __init__(self, view):
        self.view = view
        super(RustSyntaxCheckThread, self).__init__(view.window())

    def run(self):
        self.triggered_file_name = os.path.abspath(self.view.file_name())
        if util.get_setting('rust_syntax_checking_method') == 'clippy':
            # Clippy must run in the same directory as Cargo.toml.
            # See https://github.com/Manishearth/rust-clippy/issues/1515
            self.cwd = util.find_cargo_manifest(self.triggered_file_name)
            if self.cwd is None:
                print('Rust Enhanced skipping on-save syntax check.')
                print('Failed to find Cargo.toml from %r' % self.triggered_file_name)
                print('Clippy requires a Cargo.toml to exist.')
                return
        else:
            self.cwd = os.path.dirname(self.triggered_file_name)

        self.view.set_status('rust-check', 'Rust syntax check running...')
        self.this_view_found = False
        try:
            messages.clear_messages(self.window)
            try:
                self.get_rustc_messages()
            except rust_proc.ProcessTerminatedError:
                return
            messages.draw_all_region_highlights(self.window)
        finally:
            self.view.erase_status('rust-check')

    def get_rustc_messages(self):
        """Top-level entry point for generating messages for the given
        filename.

        :raises rust_proc.ProcessTerminatedError: Check was canceled.
        """
        method = util.get_setting('rust_syntax_checking_method', 'no-trans')
        if method == 'clippy':
            cmd = ['cargo', '+nightly', 'clippy', '--message-format=json']
            p = rust_proc.RustProc()
            p.run(self.window, cmd, self.cwd, self)
            p.wait()
            return

        # "no-trans" or "check" methods.
        td = target_detect.TargetDetector(self.window)
        targets = td.determine_targets(self.triggered_file_name)
        for (target_src, target_args) in targets:
            if method == 'check':
                cmd = ['cargo', 'check', '--message-format=json']
                cmd.extend(target_args)
            else:
                cmd = ['cargo', 'rustc']
                cmd.extend(target_args)
                cmd.extend(['--', '-Zno-trans', '-Zunstable-options',
                            '--error-format=json'])
                if util.get_setting('rust_syntax_checking_include_tests', True):
                    if not ('--test' in target_args or '--bench' in target_args):
                        # Including the test harness has a few drawbacks.
                        # missing_docs lint is disabled (see
                        # https://github.com/rust-lang/sublime-rust/issues/156)
                        # It also disables the "main function not found" error for
                        # binaries.
                        cmd.append('--test')
            p = rust_proc.RustProc()
            self.current_target_src = target_src
            p.run(self.window, cmd, self.cwd, self)
            p.wait()
            if self.this_view_found:
                break

    #########################################################################
    # ProcListner methods
    #########################################################################

    def on_begin(self, proc):
        pass

    def on_data(self, proc, data):
        # Debugging on-save checking problems requires viewing output here,
        # but it is difficult to segregate useful messages (like "thread
        # 'main' panicked") from all the other output.  Perhaps make a debug
        # print setting?
        pass

    def on_error(self, proc, message):
        print('Rust Error: %s' % message)

    def on_json(self, proc, obj):
        messages.add_rust_messages(self.window, self.cwd, obj,
                                   self.current_target_src, msg_cb=None)
        if messages.has_message_for_path(self.window,
                                         self.triggered_file_name):
            self.this_view_found = True

    def on_finished(self, proc, rc):
        pass

    def on_terminated(self, proc):
        pass
