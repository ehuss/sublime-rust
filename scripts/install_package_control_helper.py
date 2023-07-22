import sublime
import sys
import subprocess
import os
import logging

class StdioSplitter:
    def __init__(self, io, stream):
        self.io = io
        self.stream = stream

    def write(self, data):
        self.io.write(data)
        self.stream.write(data)

    def writeln(self, s):
        self.write(s + "\n")

    def flush(self):
        self.io.flush()
        self.stream.flush()


def plugin_loaded():

    pc_settings = sublime.load_settings("Package Control.sublime-settings")

    logfile = os.path.join(
        sublime.packages_path(),
        "0_install_package_control_helper",
        "log")

    def append(message):
        with open(logfile, "a") as f:
            f.write(message + "\n")

    append("plugin_loaded")
    append('pc_settings=%r' % (pc_settings.to_dict(),))

    stdout = sys.stdout
    stderr = sys.stderr

    stream = open(logfile, "a")
    handler = logging.StreamHandler(stream)
    logging.root.addHandler(handler)

    sys.stdout = StdioSplitter(stdout, stream)
    sys.stderr = StdioSplitter(stderr, stream)

    def kill_subl(restart=False):
        append('kill_sub(restart=%s)' % (restart,))
        if sublime.platform() == "osx":
            cmd = "sleep 1; pkill [Ss]ubl; pkill plugin_host; sleep 1; "
            if restart:
                cmd = cmd + "osascript -e 'tell application \"Sublime Text\" to activate'"
        elif sublime.platform() == "linux":
            cmd = "sleep 1; pkill [Ss]ubl; pkill plugin_host; sleep 1; "
            if restart:
                cmd = cmd + "subl"
        elif sublime.platform() == "windows":
            cmd = "sleep 1 & taskkill /F /im sublime_text.exe & sleep 1 "
            if restart:
                cmd = cmd + "& \"{}\"".format(sublime.executable_path())

        subprocess.Popen(cmd, shell=True)

    def touch(file_name):
        f = os.path.join(
            sublime.packages_path(),
            "0_install_package_control_helper",
            file_name)
        open(f, 'a').close()

    def check_bootstrap():
        append('check_bootstrap')
        if pc_settings.get("bootstrapped", False):
            touch("bootstrapped")
            kill_subl(True)
        else:
            sublime.set_timeout(check_bootstrap, 5000)

    def check_dependencies():
        append('check_dependencies')
        if 'Package Control' in sys.modules:
            append('found package control')
            package_control = sys.modules['Package Control'].package_control
        else:
            append('package control not yet found, try again')
            sublime.set_timeout(check_dependencies, 5000)
            return

        manager = package_control.package_manager.PackageManager()
        required_dependencies = set(manager.find_required_dependencies())

        class myPackageCleanup(package_control.package_cleanup.PackageCleanup):

            def finish(self, installed_packages, found_packages, found_dependencies):
                append('finish installed=%r found=%r found_deps=%r' % (installed_packages, found_packages, found_dependencies))
                missing_dependencies = required_dependencies - set(found_dependencies)

                if len(missing_dependencies) == 0:
                    append('success')
                    touch("success")
                    kill_subl()
                else:
                    with open(logfile, "a") as f:
                        f.write("missing dependencies:" + "\n")
                        f.write(" ".join(missing_dependencies) + "\n")
                    sublime.set_timeout(_check_dependencies, 5000)

        def _check_dependencies():
            myPackageCleanup().run()

        _check_dependencies()

    if not os.path.exists(os.path.join(
            sublime.packages_path(),
            "0_install_package_control_helper",
            "bootstrapped")):
        check_bootstrap()
    else:
        # restart sublime when `sublime.error_message` is run
        def error_message(message):
            with open(logfile, "a") as f:
                f.write(message + "\n")

            kill_subl(True)

        sublime.error_message = error_message
        check_dependencies()
