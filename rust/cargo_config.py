"""Sublime commands for configuring Cargo execution.

See `cargo_settings` for more details on how settings work.
"""

import getpass
import os
import re
import sublime
import sublime_plugin
from .cargo_settings import CargoSettings, CARGO_COMMANDS
from .util import index_with
from . import rust_proc, util

# Keep track of recent choices to set the default value.
RECENT_CHOICES = {}


class CancelCommandError(Exception):
    """Raised when the command should stop."""


class CargoConfigBase(sublime_plugin.WindowCommand):

    """Base class for cargo config commands.

    This implements a simple interactive UI by asking the user a series of
    questions using the Sublime quick panels for selecting choices. Subclasses
    set the `sequence` class variable to the list of questions they want to
    ask.  The choices for each question are produced by methods starting with
    'items_'+name.  These methods should return a dictionary with:

    - `items`: List of choices.  Each element should be a tuple
      `(display_string, value)`.
    - `default`: The default value (optional).
    - `skip_if_one`: Skip this question if there is only 1 item.

    `items_` methods can also just return the 'items' list.

    An optional method `selected_`+name will be called when a choice is made.
    This method can return a list of questions to be asked.

    The `done` method is called once all questions have been asked.

    Callers are allowed to pass in values instead of using the interactive UI.
    This is probably only useful for the test code, but in theory you could
    define key bindings that perform certain actions.
    """

    # CargoSettings object.
    settings = None
    # Dictionary of choices passed into the command, instead of using
    # interactive UI.
    input = None

    # Sequence of questions to ask.
    sequence = None
    # Current question being asked.
    sequence_index = 0
    # Dictionary of selections made during the interactive process.
    choices = None
    # If True, the command wants the 'package' choice to fetch metadata from
    # Cargo.
    package_wants_metadata = True
    # This is a dictionary populated by the `items_package` method.
    # Key is the path to a package, the value is the metadata from Cargo.
    # This is used by other questions (like `items_target`) to get more
    # information about the chosen package.
    packages = None

    def run(self, **kwargs):
        self.choices = {}
        self.sequence_index = 0
        # Copy, since WindowCommand reuses objects.
        self._sequence = self.sequence[:]
        self.input = kwargs
        self.settings = CargoSettings(self.window)
        self.settings.load()
        self.show_next_question()

    def done(self):
        """Called once all questions have been asked.  Subclasses must
        implement this."""
        raise NotImplementedError()

    def show_next_question(self):
        if self.sequence_index < len(self._sequence):
            q = self._sequence[self.sequence_index]
            self.sequence_index += 1
        else:
            self.done()
            return

        try:
            item_info = getattr(self, 'items_' + q)()
        except CancelCommandError:
            return
        if not isinstance(item_info, dict):
            item_info = {'items': item_info}

        f_selected = getattr(self, 'selected_' + q, None)

        def make_choice(value):
            self.choices[q] = value
            if f_selected:
                try:
                    next = f_selected(value)
                except CancelCommandError:
                    return
                if next:
                    i = self.sequence_index
                    self._sequence[i:i] = next
            self.show_next_question()

        if q in self.input:
            make_choice(self.input[q])
        else:
            if 'items' in item_info:
                def wrapper(index):
                    if index != -1:
                        chosen = item_info['items'][index][1]
                        RECENT_CHOICES[q] = chosen
                        make_choice(chosen)

                items = item_info['items']
                if item_info.get('skip_if_one', False) and len(items) == 1:
                    wrapper(0)
                else:
                    # If the user manually edits the config and enters custom
                    # values then it won't show up in the list (because it is
                    # not an exact match).  Add it so that it is a valid
                    # choice (assuming the user entered a valid value).
                    if 'default' in item_info:
                        default_index = index_with(items,
                            lambda x: x[1] == item_info['default'])
                        if default_index == -1:
                            items.append((item_info['default'],
                                          item_info['default']))
                    # Determine the default selection.
                    # Use the default provided by the items_ method, else
                    # use the most recently used value.
                    default = index_with(items,
                        lambda x: x[1] == item_info.get('default',
                            RECENT_CHOICES.get(q, '_NO_DEFAULT_SENTINEL_')))
                    display_items = [x[0] for x in items]
                    self.window.show_quick_panel(display_items, wrapper, 0,
                                                 default)
            elif 'caption' in item_info:
                self.window.show_input_panel(item_info['caption'],
                                             item_info.get('default', ''),
                                             make_choice, None, None)
            else:
                raise ValueError(item_info)

    def items_package(self):
        # If there is a manifest under the current view, use that by default.
        view = self.window.active_view()
        if view.file_name():
            manifest_dir = util.find_cargo_manifest(view.file_name())
            if manifest_dir:
                if self.package_wants_metadata:
                    metadata = get_cargo_metadata(self.window, manifest_dir)
                    if metadata:
                        for package in metadata['packages']:
                            package_dir = os.path.dirname(package['manifest_path'])
                            if package_dir == manifest_dir:
                                self.packages = {
                                    manifest_dir: package
                                }
                return {
                    'items': [(manifest_dir, manifest_dir)],
                    'skip_if_one': True,
                }

        # Otherwise, hunt for all manifest files and show a list.
        folders = self.window.folders()
        self.packages = {}
        for folder in folders:
            folder_parent = os.path.dirname(folder)
            for dirpath, dirs, files, in os.walk(folder):
                for exclude in ('.git', '.svn'):
                    if exclude in dirs:
                        dirs.remove(exclude)
                if 'Cargo.toml' in files:
                    metadata = get_cargo_metadata(self.window, dirpath)
                    if metadata:
                        for package in metadata['packages']:
                            manifest_dir = os.path.dirname(package['manifest_path'])
                            rel = os.path.relpath(manifest_dir, folder_parent)
                            package['sublime_relative'] = rel
                            if manifest_dir not in self.packages:
                                self.packages[manifest_dir] = package
                    else:
                        # Manifest load failure, let it slide.
                        print('Failed to load Cargo manifest in %r' % dirpath)

        if len(self.packages) == 0:
            sublime.error_message(util.multiline_fix("""
                Error: Cannot determine Rust package to use.

                Open a Rust file to determine which package to use, or add a folder with a Cargo.toml file to your Sublime project."""))
            raise CancelCommandError

        def display_name(package):
            return ['Package: %s' % (package['name'],),
                    package['sublime_relative']]

        items = [(display_name(package), path)
            for path, package in self.packages.items()]
        items.sort(key=lambda x: x[0])
        return {
            'items': items,
            'skip_if_one': True,
        }

    def items_target(self):
        # Group by kind.
        kinds = {}
        package_path = self.choices['package']
        for target in self.packages[package_path]['targets']:
            # AFAIK, when there are multiple "kind" values, this only happens
            # when there are multiple library kinds.
            kind = target['kind'][0]
            if kind in ('lib', 'rlib', 'dylib', 'staticlib', 'proc-macro'):
                kinds.setdefault('lib', []).append(('Lib', '--lib'))
            elif kind in ('bin', 'test', 'example', 'bench'):
                text = '%s: %s' % (kind.capitalize(), target['name'])
                arg = '--%s %s' % (kind, target['name'])
                kinds.setdefault(kind, []).append((text, arg))
            elif kind in ('custom-build',):
                # build.rs, can't be built explicitly.
                pass
            else:
                print('Rust: Unsupported target found: %s' % kind)
        items = [('All Targets', None)]
        for kind, values in kinds.items():
            allowed = True
            if self.choices.get('variant', None):
                cmd = CARGO_COMMANDS[self.choices['variant']]
                target_types = cmd['allows_target']
                if target_types is not True:
                    allowed = kind in target_types
            if allowed:
                items.extend(values)
        return items

    def items_variant(self):
        result = []
        for key, info in CARGO_COMMANDS.items():
            if self.filter_variant(info):
                result.append((info['name'], key))
        result.sort()
        return result

    def filter_variant(self, x):
        return True


class CargoConfigPackage(CargoConfigBase):

    """This is a fake command used by cargo_build to reuse the code to choose
    a Cargo package."""

    sequence = ['package']
    package_wants_metadata = False

    def run(self, on_done):
        self._on_done = on_done
        super(CargoConfigPackage, self).run()

    def done(self):
        self._on_done(self.choices['package'])


class CargoSetProfile(CargoConfigBase):

    sequence = ['package', 'target', 'profile']

    def items_profile(self):
        default = self.settings.get_with_target(self.choices['package'],
                                                self.choices['target'],
                                                'release', False)
        if default:
            default = 'release'
        else:
            default = 'dev'
        items = [('Dev', 'dev'),
                 ('Release', 'release')]
        return {'items': items,
                'default': default}

    def done(self):
        self.settings.set_with_target(self.choices['package'],
                                      self.choices['target'],
                                      'release',
                                      self.choices['profile'] == 'release')


class CargoSetTarget(CargoConfigBase):

    sequence = ['variant', 'package', 'target']

    def filter_variant(self, info):
        return info.get('allows_target', False)

    def items_target(self):
        items = super(CargoSetTarget, self).items_target()
        items.insert(1, ('Automatic Detection', 'auto'))
        default = self.settings.get_with_variant(self.choices['package'],
                                                 self.choices['variant'],
                                                 'target')
        return {
            'items': items,
            'default': default
        }

    def done(self):
        self.settings.set_with_variant(self.choices['package'],
                                       self.choices['variant'],
                                       'target',
                                       self.choices['target'])


class CargoSetTriple(CargoConfigBase):

    sequence = ['package', 'target', 'target_triple']

    def items_target_triple(self):
        # Could check if rustup is not installed, to run
        # "rustc --print target-list", but that does not tell
        # us which targets are installed.
        triples = rust_proc.check_output(self.window,
            'rustup target list'.split(), self.choices['package'])\
            .splitlines()
        current = self.settings.get_with_target(self.choices['package'],
                                                self.choices['target'],
                                                'target_triple')
        result = [('Use Default', None)]
        for triple in triples:
            if triple.endswith(' (default)'):
                actual_triple = triple[:-10]
                result.append((actual_triple, actual_triple))
            elif triple.endswith(' (installed)'):
                actual_triple = triple[:-12]
                result.append((actual_triple, actual_triple))
            else:
                actual_triple = None
            # Don't bother listing uninstalled targets.
        return {
            'items': result,
            'default': current
        }

    def done(self):
        self.settings.set_with_target(self.choices['package'],
                                      self.choices['target'],
                                      'target_triple',
                                      self.choices['target_triple'])


class CargoSetToolchain(CargoConfigBase):

    sequence = ['which']

    def items_which(self):
        return [
            ('Set Toolchain for Build Variant', 'variant'),
            ('Set Toolchain for Targets', 'target')
        ]

    def selected_which(self, which):
        if which == 'variant':
            return ['package', 'variant', 'toolchain']
        elif which == 'target':
            return ['package', 'target', 'toolchain']
        else:
            raise AssertionError(which)

    def items_toolchain(self):
        items = [('Use Default Toolchain', None)]
        toolchains = self._toolchain_list()
        if self.choices['which'] == 'variant':
            current = self.settings.get_with_variant(self.choices['package'],
                                                     self.choices['variant'],
                                                     'toolchain')
        elif self.choices['which'] == 'target':
            current = self.settings.get_with_target(self.choices['package'],
                                                    self.choices['target'],
                                                    'toolchain')
        else:
            raise AssertionError(self.choices['which'])
        items.extend([(x, x) for x in toolchains])
        return {
            'items': items,
            'default': current
        }

    def _toolchain_list(self):
        output = rust_proc.check_output(self.window,
                                        'rustup toolchain list'.split(),
                                        self.choices['package'])
        output = output.splitlines()
        system_default = index_with(output, lambda x: x.endswith(' (default)'))
        if system_default != -1:
            output[system_default] = output[system_default][:-10]
        # Rustup supports some shorthand of either `channel` or `channel-date`
        # without the trailing target info.
        #
        # Complete list of available toolchains is available at:
        # https://static.rust-lang.org/dist/index.html
        # (See https://github.com/rust-lang-nursery/rustup.rs/issues/215)
        shorthands = []
        channels = ['nightly', 'beta', 'stable', '\d\.\d{1,2}\.\d']
        pattern = '(%s)(?:-(\d{4}-\d{2}-\d{2}))?(?:-(.*))' % '|'.join(channels)
        for toolchain in output:
            m = re.match(pattern, toolchain)
            # Should always match.
            if m:
                channel = m.group(1)
                date = m.group(2)
                if date:
                    shorthand = '%s-%s' % (channel, date)
                else:
                    shorthand = channel
                if shorthand not in shorthands:
                    shorthands.append(shorthand)
        result = shorthands + output
        result.sort()
        return result

    def done(self):
        if self.choices['which'] == 'variant':
            self.settings.set_with_variant(self.choices['package'],
                                           self.choices['variant'],
                                           'toolchain',
                                           self.choices['toolchain'])
        elif self.choices['which'] == 'target':
            self.settings.set_with_target(self.choices['package'],
                                          self.choices['target'],
                                          'toolchain',
                                          self.choices['toolchain'])
        else:
            raise AssertionError(self.choices['which'])


class CargoSetFeatures(CargoConfigBase):

    sequence = ['package', 'target', 'no_default_features', 'features']

    def items_no_default_features(self):
        current = self.settings.get_with_target(self.choices['package'],
                                                self.choices['target'],
                                                'no_default_features', False)
        items = [
            ('Include default features.', False),
            ('Do not include default features.', True)
        ]
        return {
            'items': items,
            'default': current,
        }

    def items_features(self):
        features = self.settings.get_with_target(self.choices['package'],
                                                 self.choices['target'],
                                                 'features', None)
        if features is None:
            package_path = self.choices['package']
            available_features = self.packages[package_path].get('features', {})
            items = list(available_features.keys())
            # Remove the "default" entry.
            if 'default' in items:
                del items[items.index('default')]
                if not self.choices['no_default_features']:
                    # Don't show default features, (they are already included).
                    for ft in available_features['default']:
                        if ft in items:
                            del items[items.index(ft)]
            features = ' '.join(items)
        return {
            'caption': 'Choose features (space separated, use "ALL" to use all features)',
            'default': features,
        }

    def done(self):
        self.settings.set_with_target(self.choices['package'],
                                      self.choices['target'],
                                      'no_default_features',
                                      self.choices['no_default_features'])
        self.settings.set_with_target(self.choices['package'],
                                      self.choices['target'],
                                      'features',
                                      self.choices['features'])


class CargoCreateNewBuild(CargoConfigBase):

    """Command to create a new build variant, stored in the user's
    `.sublime-project` file."""

    sequence = ['command']

    def items_command(self):
        if self.window.project_data() is None:
            sublime.error_message(util.multiline_fix("""
                Error: This command requires a .sublime-project file.

                Save your Sublime project and try again."""))
            raise CancelCommandError
        result = []
        for key, info in CARGO_COMMANDS.items():
            result.append((info['name'], key))
        result.sort()
        result.append(('New Command', 'NEW_COMMAND'))
        return result

    def selected_command(self, command):
        if command == 'NEW_COMMAND':
            return ['new_command', 'allows_target', 'allows_target_triple',
                'allows_release', 'allows_features', 'allows_json',
                'requires_manifest', 'requires_view_path', 'wants_run_args',
                'name']
        else:
            cinfo = CARGO_COMMANDS[command]
            result = []
            if cinfo.get('requires_manifest', True):
                result.append('package')
            result.append('name')
            return result

    def items_package(self):
        result = super(CargoCreateNewBuild, self).items_package()
        if len(result['items']) > 1:
            result['items'].insert(0, (['Any Package',
                'This build variant is not tied to any particular Cargo package.'],
                None))
        return result

    def selected_package(self, package):
        if package:
            cinfo = CARGO_COMMANDS[self.choices['command']]
            if cinfo.get('allows_target', False):
                return ['target']

    def items_new_command(self):
        return {
            'caption': 'Enter the Cargo subcommand to run:',
        }

    def selected_new_command(self, command):
        if not command:
            sublime.error_message('Error: You must enter a command to run.')
            raise CancelCommandError

    def items_allows_target(self):
        return [
            ('Command %r supports Cargo filters (--bin, --example, etc.)' % (
                self.choices['new_command']), True),
            ('Command %r does not support target filters' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_target_triple(self):
        return [
            ('Command %r supports --target triple flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --target' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_release(self):
        return [
            ('Command %r supports --release flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --release' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_features(self):
        return [
            ('Command %r supports --features flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support --features' % (
                self.choices['new_command'],), False)
        ]

    def items_allows_json(self):
        return [
            ('Command %r supports --message-format=json flag' % (
                self.choices['new_command']), True),
            ('Command %r does not support JSON' % (
                self.choices['new_command'],), False)
        ]

    def items_requires_manifest(self):
        return [
            ('Command %r requires a Cargo.toml manifest' % (
                self.choices['new_command']), True),
            ('Command %r does not require a manifest' % (
                self.choices['new_command'],), False)
        ]

    def items_requires_view_path(self):
        return [
            ('Do not include view path', False),
            ('Include path of active sublime view on command line', True),
        ]

    def items_wants_run_args(self):
        return [
            ('Do not ask for more arguments', False),
            ('Ask for extra command-line arguments each time', True),
        ]

    def items_name(self):
        name = '%s\'s %s' % (getpass.getuser(),
            self.choices.get('new_command', self.choices['command']))
        target = self.choices.get('target', None)
        if target:
            target = target.replace('-', '')
            name = name + ' %s' % (target,)
        return {
            'caption': 'Enter a name for your new Cargo build system:',
            'default': name
        }

    def selected_name(self, name):
        if not name:
            sublime.error_message('Error: You must enter a name.')
            raise CancelCommandError

    def done(self):
        proj_data = self.window.project_data()
        systems = proj_data.setdefault('build_systems', [])
        for system_index, system in enumerate(systems):
            if system.get('target') == 'cargo_exec':
                break
        else:
            system = self._stock_build_system()
            system['name'] = 'Custom Cargo Build'
            system_index = len(systems)
            systems.append(system)
        variants = system.setdefault('variants', [])

        # Add the defaults to make it easier to manually edit.
        settings = {
            'release': False,
            'target_triple': '',
            'toolchain': '',
            'target': '',
            'no_default_features': False,
            'features': '',
            'extra_cargo_args': '',
            'extra_run_args': '',
            'env': {},
        }
        cinfo = {}
        result = {
            'name': self.choices['name'],
            'target': 'cargo_exec',
            'command': self.choices.get('new_command',
                                        self.choices['command']),
            'settings': settings,
            'command_info': cinfo,
        }
        if self.choices['command'] == 'NEW_COMMAND':
            for key in ['allows_target', 'allows_target_triple',
                        'allows_release', 'allows_features', 'allows_json',
                        'requires_manifest', 'requires_view_path',
                        'wants_run_args']:
                cinfo[key] = self.choices[key]
            requires_view_path = cinfo.get('requires_view_path')
        else:
            if 'target' in self.choices:
                settings['target'] = self.choices['target']
            if 'package' in self.choices:
                settings['working_dir'] = self.choices['package']
            requires_view_path = CARGO_COMMANDS[self.choices['command']]\
                .get('requires_view_path', False)

        if requires_view_path and util.active_view_is_rust():
            settings['script_path'] = self.window.active_view().file_name()

        variants.insert(0, result)
        self.window.set_project_data(proj_data)
        self.window.run_command('set_build_system', {'index': system_index})

    def _stock_build_system(self):
        pkg_name = __name__.split('.')[0]
        resource = 'Packages/%s/RustEnhanced.sublime-build' % pkg_name
        return sublime.decode_value(sublime.load_resource(resource))


def get_cargo_metadata(window, cwd):
    """Load Cargo metadata.

    :returns: None on failure, otherwise a dictionary from Cargo:
        - packages: List of packages:
            - name
            - manifest_path: Path to Cargo.toml.
            - targets: List of target dictionaries:
                - name: Name of target.
                - src_path: Path of top-level source file.  May be a
                  relative path.
                - kind: List of kinds.  May contain multiple entries if
                  `crate-type` specifies multiple values in Cargo.toml.
                  Lots of different types of values:
                    - Libraries: 'lib', 'rlib', 'dylib', 'staticlib',
                      'proc-macro'
                    - Executables: 'bin', 'test', 'example', 'bench'
                    - build.rs: 'custom-build'

    :raises ProcessTermiantedError: Process was terminated by another thread.
    """
    output = rust_proc.slurp_json(window,
                                  'cargo metadata --no-deps'.split(),
                                  cwd=cwd)
    if output:
        return output[0]
    else:
        return None
