"""Module for storing/displaying Rust compiler messages."""

import sublime

import collections
import functools
import html
import itertools
import os
import re
import urllib.parse
import uuid
import webbrowser

from . import util

# Key is window id.
# Value is a dictionary: {
#     'paths': {path: [Message, ...]},
#     'msg_index': (path_idx, message_idx),
# }
# `paths` is an OrderedDict to handle next/prev message.
# `path` is the absolute path to the file.
WINDOW_MESSAGES = {}


LINK_PATTERN = r'(https?://[-a-zA-Z0-9@:%._+~#=]{2,256}\.[a-zA-Z]{2,6}\b[-a-zA-Z0-9@:%_+.~#?&/=]*)'


CSS_TEMPLATE = """
<style>
    span {{
        font-family: monospace;
    }}
    .rust-error {{
        color: {error_color};
    }}
    .rust-warning {{
        color: {warning_color};
    }}
    .rust-note {{
        color: {note_color};
    }}
    .rust-help {{
        color: {help_color};
    }}
    .rust-link {{
        background-color: var(--background);
        color: var(--bluish);
        text-decoration: none;
        border-radius: 1rem;
        padding: 0.2rem 0.5rem;
        border: 1px solid var(--bluish);
    }}
    .rust-links {{
        margin: 0.4rem 0rem;
    }}
    a {{
        text-decoration: inherit;
        padding: 0.35rem 0.5rem 0.45rem 0.5rem;
        position: relative;
        font-weight: bold;
    }}
    {extra_css}
</style>
<body id="rust-message">
{content}
</body>
"""

POPUP_CSS = """
    body {
        margin: 0.25em;
    }
"""


class Message:

    """A diagnostic message.

    :ivar id: A unique uuid for this message.
    :ivar region_key: A string for the Sublime highlight region and phantom
        for this message.  Unique per view.
    :ivar text: The raw text of the message without any minihtml markup.  May
        be None if the content is raw markup (such as a minihtml link) or if
        it is an outline-only region (which happens with things such as
        dual-region messages added in 1.21).
    :ivar minihtml_text: The string used for showing phantoms that includes
        the minihtml markup.  May be None.
    :ivar level: Message level as a string such as "error", or "info".
    :ivar span: Location of the message (0-based):
        `((line_start, col_start), (line_end, col_end))`
        May be `None` to indicate no particular spot.
    :ivar path: Absolute path to the file.
    :ivar code: Rust error code as a string such as 'E0001'.  May be None.
    :ivar output_panel_region: Optional Sublime Region object that indicates
        the region in the build output panel that corresponds with this message.
    :ivar back_link: Optional string of HTML code that is a link back to the
        main message.
    :ivar primary: True if this is the primary message, False if a child.
    :ivar hidden: Boolean if this message should be displayed.
    :ivar children: List of additional Message objects.  This is *not*
        recursive (children cannot have children).
    :ivar parent: The primary message if this a child.
    """
    region_key = None
    text = None
    minihtml_text = None
    level = None
    span = None
    path = None
    code = None
    output_panel_region = None
    back_link = None
    primary = True
    parent = None
    hidden = False

    def __init__(self):
        self.id = uuid.uuid4()
        self.children = []

    def lineno(self):
        """Return the line number of the message."""
        if self.span:
            return self.span[0][0]
        else:
            return 999999999

    def __iter__(self):
        # Convenience iterator for iterating over the message and its children.
        yield self
        for child in self.children:
            yield child

    def is_similar(self, other):
        keys = ('path', 'span', 'level', 'text')
        for key in keys:
            if getattr(other, key) != getattr(self, key):
                return False
        else:
            return True

    def sublime_region(self, view):
        """Returns a sublime.Region object for this message."""
        if self.span:
            return sublime.Region(
                view.text_point(self.span[0][0], self.span[0][1]),
                view.text_point(self.span[1][0], self.span[1][1])
            )
        else:
            # Place at bottom of file for lack of anywhere better.
            return sublime.Region(view.size())

    def dismiss(self, window):
        """Permanently remove this message and all its children from the
        view."""
        if self.parent:
            return self.parent.dismiss(window)
        for msg in self:
            # There is a awkward problem with Sublime and
            # add_regions/erase_regions. The regions are part of the undo
            # stack, which means even after we erase them, they can come back
            # from the dead if the user hits undo. We simply mark these as
            # "hidden" to ensure that `clear_messages` can erase any of these
            # zombie regions.  See
            # https://github.com/SublimeTextIssues/Core/issues/1121
            msg.hidden = True
            view = window.find_open_file(msg.path)
            if view:
                view.erase_regions(msg.region_key)
                view.erase_phantoms(msg.region_key)

    def __repr__(self):
        result = ['<Message\n']
        for key, value in self.__dict__.items():
            if key == 'parent':
                result.append('    parent=%r\n' % (value.id,))
            else:
                result.append('    %s=%r\n' % (key, value))
        result.append('>')
        return ''.join(result)


def clear_messages(window):
    for path, messages in WINDOW_MESSAGES.pop(window.id(), {})\
                                         .get('paths', {})\
                                         .items():
        view = window.find_open_file(path)
        if view:
            for message in messages:
                view.erase_regions(message.region_key)
                view.erase_phantoms(message.region_key)


def clear_all_messages():
    for window in sublime.windows():
        if window.id() in WINDOW_MESSAGES:
            clear_messages(window)


def add_message(window, message, msg_cb=None):
    """Add a message to be displayed (ignores children).

    :param window: The Sublime window.
    :param message: The `Message` object to add.
    :param msg_cb: Callback that will be given the message.
    """
    if 'macros>' in message.path:
        # Macros from external crates will be displayed in the console
        # via msg_cb.
        return
    wid = window.id()
    try:
        messages_by_path = WINDOW_MESSAGES[wid]['paths']
    except KeyError:
        messages_by_path = collections.OrderedDict()
        WINDOW_MESSAGES[wid] = {
            'paths': messages_by_path,
            'msg_index': (-1, -1)
        }
    messages = messages_by_path.setdefault(message.path, [])
    for other in messages:
        if message.is_similar(other):
            return
    messages.append(message)
    message.region_key = 'rust-%i' % (len(messages),)

    view = window.find_open_file(message.path)
    if view:
        _show_phantom(view, message)
    if msg_cb:
        msg_cb(message)


def has_message_for_path(window, path):
    paths = WINDOW_MESSAGES.get(window.id(), {}).get('paths', {})
    return path in paths


def messages_finished(window):
    """This should be called after all messages have been added."""
    _sort_messages(window)
    _draw_all_region_highlights(window)


def _draw_all_region_highlights(window):
    """Drawing region outlines must be deferred until all the messages have
    been received since Sublime does not have an API to incrementally add
    them."""
    paths = WINDOW_MESSAGES.get(window.id(), {}).get('paths', {})
    for path, messages in paths.items():
        view = window.find_open_file(path)
        if view:
            _draw_region_highlights(view, messages)


def _draw_region_highlights(view, messages):
    if util.get_setting('rust_region_style', 'outline') == 'none':
        return

    regions = {
        'error': [],
        'warning': [],
        'note': [],
        'help': [],
    }
    for message in messages:
        if message.hidden:
            continue
        region = message.sublime_region(view)
        if message.level not in regions:
            print('RustEnhanced: Unknown message level %r encountered.' % message.level)
            message.level = 'error'
        regions[message.level].append((message.region_key, region))

    # Remove lower-level regions that are identical to higher-level regions.
    # def filter_out(to_filter, to_check):
    #     def check_in(region):
    #         for r in regions[to_check]:
    #             if r == region:
    #                 return False
    #         return True
    #     regions[to_filter] = list(filter(check_in, regions[to_filter]))
    # filter_out('help', 'note')
    # filter_out('help', 'warning')
    # filter_out('help', 'error')
    # filter_out('note', 'warning')
    # filter_out('note', 'error')
    # filter_out('warning', 'error')

    package_name = __package__.split('.')[0]
    gutter_style = util.get_setting('rust_gutter_style', 'shape')

    # Do this in reverse order so that errors show on-top.
    for level in ['help', 'note', 'warning', 'error']:
        # Unfortunately you cannot specify colors, but instead scopes as
        # defined in the color theme.  If the scope is not defined, then it
        # will show up as foreground color (white in dark themes).  I just use
        # "info" as an undefined scope (empty string will remove regions).
        # "invalid" will typically show up as red.
        if level == 'error':
            scope = 'invalid'
        else:
            scope = 'info'
        if gutter_style == 'none':
            icon = ''
        else:
            icon = 'Packages/%s/images/gutter/%s-%s.png' % (
                package_name, gutter_style, level)
        for key, region in regions[level]:
            _sublime_add_regions(
                view, key, [region], scope, icon,
                sublime.DRAW_NO_FILL | sublime.DRAW_EMPTY)


def _wrap_css(content, extra_css=''):
    """Takes the given minihtml content and places it inside a <body> with the
    appropriate CSS."""
    return CSS_TEMPLATE.format(content=content,
        error_color=util.get_setting('rust_syntax_error_color', 'var(--redish)'),
        warning_color=util.get_setting('rust_syntax_warning_color', 'var(--yellowish)'),
        note_color=util.get_setting('rust_syntax_note_color', 'var(--greenish)'),
        help_color=util.get_setting('rust_syntax_help_color', 'var(--bluish)'),
        extra_css=extra_css,
    )


def message_popup(view, point, hover_zone):
    """Displays a popup if there is a message at the given point."""
    paths = WINDOW_MESSAGES.get(view.window().id(), {}).get('paths', {})
    msgs = paths.get(view.file_name(), [])

    if hover_zone == sublime.HOVER_GUTTER:
        # Collect all messages on this line.
        row = view.rowcol(point)[0]

        def filter_row(msg):
            span = msg.span
            if span:
                return row >= span[0][0] and row <= span[1][0]
            else:
                last_row = view.rowcol(view.size())[0]
                return row == last_row

        msgs = filter(filter_row, msgs)
    else:
        # Collect all messages covering this point.
        def filter_point(msg):
            span = msg.span
            if span:
                start_pt = view.text_point(*span[0])
                end_pt = view.text_point(*span[1])
                return point >= start_pt and point <= end_pt
            else:
                return point == view.size()

        msgs = filter(filter_point, msgs)

    if msgs:
        to_show = '\n'.join(msg.minihtml_text for msg in msgs if msg.minihtml_text)
        minihtml = _wrap_css(to_show, extra_css=POPUP_CSS)
        on_nav = functools.partial(_click_handler, view, hide_popup=True)
        max_width = view.em_width() * 79
        view.show_popup(minihtml, sublime.COOPERATE_WITH_AUTO_COMPLETE,
            point, max_width=max_width, on_navigate=on_nav)


def _click_handler(view, url, hide_popup=False):
    if url == 'hide':
        clear_messages(view.window())
        if hide_popup:
            view.hide_popup()
    elif url.startswith('file:///'):
        view.window().open_file(url[8:], sublime.ENCODED_POSITION)
    elif url.startswith('replace:'):
        info = urllib.parse.parse_qs(url[8:])
        _accept_replace(view, info['id'][0], info['replacement'][0])
        if hide_popup:
            view.hide_popup()
    else:
        webbrowser.open_new(url)


def _accept_replace(view, mid, replacement):
    msgs = WINDOW_MESSAGES.get(view.window().id(), {})\
        .get('paths', {})\
        .get(view.file_name(), [])
    for msg in msgs:
        if str(msg.id) == mid:
            break
    else:
        print('Rust Enhanced internal error: Could not find ID %r' % (mid,))
        return
    # Retrieve the updated region from sublime.
    regions = view.get_regions(msg.region_key)
    if not regions:
        print('Rust Enhanced internal error: Could not find region for suggestion.')
        return
    region = (regions[0].a, regions[0].b)
    msg.dismiss(view.window())
    view.run_command('rust_accept_suggested_replacement', {
        'region': region,
        'replacement': replacement
    })


def _show_phantom(view, message):
    if util.get_setting('rust_phantom_style', 'normal') != 'normal':
        return
    if message.hidden or not message.minihtml_text:
        return

    region = message.sublime_region(view)
    # For some reason if you have a multi-line region, the phantom is only
    # displayed under the first line.  I think it makes more sense for the
    # phantom to appear below the last line.
    start = view.rowcol(region.begin())
    end = view.rowcol(region.end())
    if start[0] != end[0]:
        # Spans multiple lines, adjust to the last line.
        region = sublime.Region(
            view.text_point(end[0], 0),
            region.end()
        )

    _sublime_add_phantom(
        view,
        message.region_key, region,
        _wrap_css(message.minihtml_text),
        sublime.LAYOUT_BLOCK,
        functools.partial(_click_handler, view)
    )


def _sublime_add_phantom(view, key, region, content, layout, on_navigate):
    """Pulled out to assist testing."""
    view.add_phantom(
        key, region,
        content,
        layout,
        on_navigate
    )


def _sublime_add_regions(view, key, regions, scope, icon, flags):
    """Pulled out to assist testing."""
    view.add_regions(key, regions, scope, icon, flags)


def _sort_messages(window):
    """Sorts messages so that errors are shown first when using Next/Prev
    commands."""
    # Undocumented config variable to disable sorting in case there are
    # problems with it.
    if not util.get_setting('rust_sort_messages', True):
        return
    wid = window.id()
    try:
        window_info = WINDOW_MESSAGES[wid]
    except KeyError:
        return
    messages_by_path = window_info['paths']
    items = []
    for path, messages in messages_by_path.items():
        for message in messages:
            level = {
                'error': 0,
                'warning': 1,
                'note': 2,
                'help': 3,
            }.get(message.level, 0)
            items.append((level, path, message.lineno(), message))
    items.sort(key=lambda x: x[:3])
    messages_by_path = collections.OrderedDict()
    for _, path, _, message in items:
        messages = messages_by_path.setdefault(path, [])
        messages.append(message)
    window_info['paths'] = messages_by_path


def show_next_message(window, levels):
    current_idx = _advance_next_message(window, levels)
    _show_message(window, current_idx)


def show_prev_message(window, levels):
    current_idx = _advance_prev_message(window, levels)
    _show_message(window, current_idx)


def _show_message(window, current_idx, transient=False, force_open=False):
    if current_idx is None:
        return
    try:
        window_info = WINDOW_MESSAGES[window.id()]
    except KeyError:
        return
    paths = window_info['paths']
    path, messages = _ith_iter_item(paths.items(), current_idx[0])
    msg = messages[current_idx[1]]
    _scroll_build_panel(window, msg)
    view = None
    if not transient and not force_open:
        view = window.find_open_file(path)
        if view:
            _scroll_to_message(view, msg, transient)
    if not view:
        flags = sublime.ENCODED_POSITION
        if transient:
            # FORCE_GROUP is undocumented.  It forces the view to open in the
            # current group, even if the view is already open in another
            # group.  This is necessary to prevent the quick panel from losing
            # focus. See:
            # https://github.com/SublimeTextIssues/Core/issues/1041
            flags |= sublime.TRANSIENT | sublime.FORCE_GROUP
        if msg.span:
            # show_at_center is buggy with newly opened views (see
            # https://github.com/SublimeTextIssues/Core/issues/538).
            # ENCODED_POSITION is 1-based.
            row, col = msg.span[0]
        else:
            row, col = (999999999, 1)
        view = window.open_file('%s:%d:%d' % (path, row + 1, col + 1),
                                flags)
        # Block until the view is loaded.
        _show_message_wait(view, messages, current_idx)


def _show_message_wait(view, messages, current_idx):
    if view.is_loading():
        def f():
            _show_message_wait(view, messages, current_idx)
        sublime.set_timeout(f, 10)
    # The on_load event handler will call show_messages_for_view which
    # should handle displaying the messages.


def _scroll_build_panel(window, message):
    """If the build output panel is open, scroll the output to the message
    selected."""
    if message.output_panel_region:
        # Defer cyclic import.
        from . import opanel
        view = window.find_output_panel(opanel.PANEL_NAME)
        if view:
            view.sel().clear()
            region = message.output_panel_region
            view.sel().add(region)
            view.show(region)
            # Force panel to update.
            # TODO: See note about workaround below.
            view.add_regions('bug', [region], 'bug', 'dot', sublime.HIDDEN)
            view.erase_regions('bug')


def _scroll_to_message(view, message, transient):
    """Scroll view to the message."""
    if not transient:
        view.window().focus_view(view)
    r = message.sublime_region(view)
    view.sel().clear()
    view.sel().add(r.a)
    view.show_at_center(r)
    # TODO: Fix this to use a TextCommand to properly handle undo.
    # See https://github.com/SublimeTextIssues/Core/issues/485
    view.add_regions('bug', [r], 'bug', 'dot', sublime.HIDDEN)
    view.erase_regions('bug')


def show_messages_for_view(view):
    """Adds all phantoms and region outlines for a view."""
    window = view.window()
    paths = WINDOW_MESSAGES.get(window.id(), {}).get('paths', {})
    messages = paths.get(view.file_name(), None)
    if messages:
        _show_messages_for_view(view, messages)


def _show_messages_for_view(view, messages):
    for message in messages:
        _show_phantom(view, message)
    _draw_region_highlights(view, messages)


def _ith_iter_item(d, i):
    return next(itertools.islice(d, i, None))


def _advance_next_message(window, levels, wrap_around=False):
    """Update global msg_index to the next index."""
    try:
        win_info = WINDOW_MESSAGES[window.id()]
    except KeyError:
        return None
    paths = win_info['paths']
    path_idx, msg_idx = win_info['msg_index']
    if path_idx == -1:
        # First time.
        path_idx = 0
        msg_idx = 0
    else:
        msg_idx += 1

    while path_idx < len(paths):
        messages = _ith_iter_item(paths.values(), path_idx)
        while msg_idx < len(messages):
            msg = messages[msg_idx]
            if _is_matching_level(levels, msg):
                current_idx = (path_idx, msg_idx)
                win_info['msg_index'] = current_idx
                return current_idx
            msg_idx += 1
        path_idx += 1
        msg_idx = 0
    if wrap_around:
        # No matching entries, give up.
        return None
    else:
        # Start over at the beginning of the list.
        win_info['msg_index'] = (-1, -1)
        return _advance_next_message(window, levels, wrap_around=True)


def _last_index(paths):
    path_idx = len(paths) - 1
    msg_idx = len(_ith_iter_item(paths.values(), path_idx)) - 1
    return (path_idx, msg_idx)


def _advance_prev_message(window, levels, wrap_around=False):
    """Update global msg_index to the previous index."""
    try:
        win_info = WINDOW_MESSAGES[window.id()]
    except KeyError:
        return None
    paths = win_info['paths']
    path_idx, msg_idx = win_info['msg_index']
    if path_idx == -1:
        # First time, start at the end.
        path_idx, msg_idx = _last_index(paths)
    else:
        msg_idx -= 1

    while path_idx >= 0:
        messages = _ith_iter_item(paths.values(), path_idx)
        while msg_idx >= 0:
            msg = messages[msg_idx]
            if _is_matching_level(levels, msg):
                current_idx = (path_idx, msg_idx)
                win_info['msg_index'] = current_idx
                return current_idx
            msg_idx -= 1
        path_idx -= 1
        if path_idx >= 0:
            msg_idx = len(_ith_iter_item(paths.values(), path_idx)) - 1
    if wrap_around:
        # No matching entries, give up.
        return None
    else:
        # Start over at the end of the list.
        win_info['msg_index'] = (-1, -1)
        return _advance_prev_message(window, levels, wrap_around=True)


def _is_matching_level(levels, message):
    if not message.primary:
        # Only navigate to top-level messages.
        return False
    if levels == 'all':
        return True
    elif levels == 'error' and message.level == 'error':
        return True
    elif levels == 'warning' and message.level != 'error':
        # Warning, Note, Help
        return True
    else:
        return False


def _relative_path(window, path):
    """Convert an absolute path to a relative path used for a truncated
    display."""
    for folder in window.folders():
        if path.startswith(folder):
            return os.path.relpath(path, folder)
    return path


def list_messages(window):
    """Show a list of all messages."""
    try:
        win_info = WINDOW_MESSAGES[window.id()]
    except KeyError:
        # XXX: Or dialog?
        window.show_quick_panel(["No messages available"], None)
        return
    panel_items = []
    jump_to = []
    for path_idx, (path, msgs) in enumerate(win_info['paths'].items()):
        for msg_idx, message in enumerate(msgs):
            if not message.primary:
                continue
            jump_to.append((path_idx, msg_idx))
            if message.span:
                path_label = '%s:%s' % (
                    _relative_path(window, path),
                    message.span[0][0] + 1)
            else:
                path_label = _relative_path(window, path)
            item = [message.text, path_label]
            panel_items.append(item)

    def on_done(idx):
        _show_message(window, jump_to[idx], force_open=True)

    def on_highlighted(idx):
        _show_message(window, jump_to[idx], transient=True)

    window.show_quick_panel(panel_items, on_done, 0, 0, on_highlighted)


def add_rust_messages(window, base_path, info, target_path, msg_cb):
    """Add messages from Rust JSON to Sublime views.

    - `window`: Sublime Window object.
    - `base_path`: Base path used for resolving relative paths from Rust.
    - `info`: Dictionary of messages from rustc or cargo.
    - `target_path`: Absolute path to the top-level source file of the target
      (lib.rs, main.rs, etc.).  May be None if it is not known.
    - `msg_cb`: Function called for each message (if not None).  It is given a
      single parameter, a dictionary of the message to display with the
      following keys:
        - `path`: Full path to the file.  None if no file associated.
        - `span`: Sublime (0-based) offsets into the file for the region
          `((line_start, col_start), (line_end, col_end))`.  None if no
          region.
        - `level`: Rust level ('error', 'warning', 'note', etc.)
        - `is_main`: If True, a top-level message.
        - `text`: Raw text of the message without markup.
    """
    # cargo check emits in a slightly different format.
    if 'reason' in info:
        if info['reason'] == 'compiler-message':
            info = info['message']
        else:
            # cargo may emit various other messages, like
            # 'compiler-artifact' or 'build-script-executed'.
            return

    primary_message = Message()

    _collect_rust_messages(window, base_path, info, target_path, msg_cb, {},
        primary_message)
    if not primary_message.path:
        return
    _create_cross_links(primary_message)
    _create_minihtml(primary_message)
    for msg in primary_message:
        add_message(window, msg, msg_cb)


def _create_minihtml(primary_message):
    """Sets the `minihtml_text` field of the message and its children."""
    content_template = '<div class="{cls}">{level}{msg}{help_link}{back_link}{close_link}</div>'

    if primary_message.code:
        # TODO
        # This could potentially be a link that opens a Sublime popup, or
        # a new temp buffer with the contents of 'explanation'.
        # (maybe use sublime-markdown-popups)
        help_link = ' <a href="https://doc.rust-lang.org/error-index.html#%s">?</a>' % (
            primary_message.code,)
    else:
        help_link = ''

    last_level = None
    last_path = None
    for msg in primary_message:
        if msg.minihtml_text or not msg.text:
            continue
        cls = {
            'error': 'rust-error',
            'warning': 'rust-warning',
            'note': 'rust-note',
            'help': 'rust-help',
        }.get(msg.level, 'rust-error')
        indent = '&nbsp;' * (len(msg.level) + 2)
        if msg.level == last_level and msg.path == last_path:
            level_text = indent
        else:
            level_text = '%s: ' % (msg.level,)
        last_level = msg.level
        last_path = msg.path

        if msg.primary:
            close_link = '<a href="hide">\xD7</a>'
        else:
            close_link = ''

        def escape_and_link(i_txt):
            i, txt = i_txt
            if i % 2:
                return '<a href="%s">%s</a>' % (txt, txt)
            else:
                # Call strip() because sometimes rust includes newlines at the
                # end of the message, which we don't want.
                return html.escape(txt.strip(), quote=False).\
                    replace('\n', '<br>' + indent)

        parts = re.split(LINK_PATTERN, msg.text)
        escaped_text = ''.join(map(escape_and_link, enumerate(parts)))

        msg.minihtml_text = content_template.format(
            cls=cls,
            level=level_text,
            msg=escaped_text,
            help_link=help_link,
            back_link=msg.back_link or '',
            close_link=close_link,
        )


def _collect_rust_messages(window, base_path, info, target_path,
                           msg_cb, parent_info,
                           message):
    """
    - `info`: The dictionary from Rust has the following structure:

        - 'message': The message to display.
        - 'level': The error level ('error', 'warning', 'note', 'help')
                   (XXX I think an ICE shows up as 'error: internal compiler
                   error')
        - 'code': If not None, contains a dictionary of extra information
          about the error.
            - 'code': String like 'E0001'
            - 'explanation': Optional string with a very long description of
              the error.  If not specified, then that means nobody has gotten
              around to describing the error, yet.
        - 'spans': List of regions with diagnostic information.  May be empty
          (child messages attached to their parent, or global messages like
          "main not found"). Each element is:

            - 'file_name': Filename for the message.  For spans located in the
              'expansion' section, this will be the name of the expanded macro
              in the format '<macroname macros>'.
            - 'byte_start':
            - 'byte_end':
            - 'line_start':
            - 'line_end':
            - 'column_start':
            - 'column_end':
            - 'is_primary': If True, this is the primary span where the error
              started.  Note: It is possible (though rare) for multiple spans
              to be marked as primary (for example, 'immutable borrow occurs
              here' and 'mutable borrow ends here' can be two separate spans
              both "primary").  Top (parent) messages should always have at
              least one primary span (unless it has 0 spans).  Child messages
              may have 0 or more primary spans.  AFAIK, spans from 'expansion'
              are never primary.
            - 'text': List of dictionaries showing the original source code.
            - 'label': A message to display at this span location.  May be
              None (AFAIK, this only happens when is_primary is True, in which
              case the main 'message' is all that should be displayed).
            - 'suggested_replacement':  If not None, a string with a
              suggestion of the code to replace this span.
            - 'expansion': If not None, a dictionary indicating the expansion
              of the macro within this span.  The values are:

                - 'span': A span object where the macro was applied.
                - 'macro_decl_name': Name of the macro ("print!" or
                  "#[derive(Eq)]")
                - 'def_site_span': Span where the macro was defined (may be
                  None if not known).

        - 'children': List of attached diagnostic messages (following this
          same format) of associated information.  AFAIK, these are never
          nested.
        - 'rendered': Optional string (may be None).

          Before 1.23: Used by suggested replacements.  If
          'suggested_replacement' is set, then this is rendering of how the
          line should be written.

          After 1.23:  This contains the ASCII-art rendering of the message as
          displayed by rustc's normal console output.

    - `parent_info`: Dictionary used for tracking "children" messages.
      Currently only has 'span' key, the span of the parent to display the
      message (for children without spans).
    - `message`: `Message` object where we store the message information.
    """
    # Include "notes" tied to errors, even if warnings are disabled.
    if (info['level'] != 'error' and
        util.get_setting('rust_syntax_hide_warnings', False) and
        not parent_info
       ):
        return

    def make_span_path(span):
        return os.path.realpath(os.path.join(base_path, span['file_name']))

    def make_span_region(span):
        # Sublime text is 0 based whilst the line/column info from
        # rust is 1 based.
        if span.get('line_start'):
            return ((span['line_start'] - 1, span['column_start'] - 1),
                    (span['line_end'] - 1, span['column_end'] - 1))
        else:
            return None

    def set_primary_message(span, text):
        parent_info['span'] = span
        # Not all codes have explanations (yet).
        if info['code'] and info['code']['explanation']:
            message.code = info['code']['code']
        message.path = make_span_path(span)
        message.span = make_span_region(span)
        message.text = text
        message.level = info['level']

    def add_additional(span, text, level):
        child = Message()
        child.path = make_span_path(span)
        child.span = make_span_region(span)
        child.text = text
        child.level = level
        child.primary = False
        child.parent = message
        message.children.append(child)
        return child

    if len(info['spans']) == 0:
        if parent_info:
            # This is extra info attached to the parent message.
            add_additional(parent_info['span'], info['message'], info['level'])
        else:
            # Messages without spans are global session messages (like "main
            # function not found").
            #
            # Some of the messages are not very interesting, though.
            imsg = info['message']
            if not (imsg.startswith('aborting due to') or
                    imsg.startswith('cannot continue')):
                if target_path:
                    # Display at the bottom of the root path (like main.rs)
                    # for lack of a better place to put it.
                    fake_span = {'file_name': target_path}
                    set_primary_message(fake_span, imsg)
                else:
                    # Not displayed as a phantom since we don't know where to
                    # put it.
                    if msg_cb:
                        tmp_msg = Message()
                        tmp_msg.level = info['level']
                        tmp_msg.text = imsg
                        msg_cb(tmp_msg)

    def find_span_r(span, expansion=None):
        if span['expansion']:
            return find_span_r(span['expansion']['span'], span['expansion'])
        else:
            return span, expansion

    for span in info['spans']:
        if 'macros>' in span['file_name']:
            # Rust gives the chain of expansions for the macro, which we don't
            # really care about.  We want to find the site where the macro was
            # invoked.  I'm not entirely confident this is the best way to do
            # this, but it seems to work.  This is roughly emulating what is
            # done in librustc_errors/emitter.rs fix_multispan_in_std_macros.
            target_span, expansion = find_span_r(span)
            if not target_span:
                continue
            updated = target_span.copy()
            updated['is_primary'] = span['is_primary']
            updated['label'] = span['label']
            updated['suggested_replacement'] = span['suggested_replacement']
            span = updated

            if 'macros>' in span['file_name']:
                # Macros from extern crates do not have 'expansion', and thus
                # we do not have a location to highlight.  Place the result at
                # the bottom of the primary target path.
                macro_name = span['file_name']
                if target_path:
                    span['file_name'] = target_path
                    span['line_start'] = None
                # else, messages will be shown in console via msg_cb.
                add_additional(span,
                    'Errors occurred in macro %s from external crate' % (macro_name,),
                    info['level'])
                text = ''.join([x['text'] for x in span['text']])
                add_additional(span,
                    'Macro text: %s' % (text,),
                    info['level'])
            else:
                if not expansion or not expansion['def_site_span'] \
                        or 'macros>' in expansion['def_site_span']['file_name']:
                    add_additional(span,
                        'this error originates in a macro outside of the current crate',
                        info['level'])

        # Add a message for macro invocation site if available in the local
        # crate.
        if span['expansion'] and \
                'macros>' not in span['file_name'] and \
                not span['expansion']['macro_decl_name'].startswith('#['):
            invoke_span, expansion = find_span_r(span)
            add_additional(invoke_span, 'in this macro invocation', 'help')

        if span['is_primary']:
            if parent_info:
                # Primary child message.
                add_additional(span, info['message'], info['level'])
            else:
                # Check if the main message is already set since there might
                # be multiple spans that are primary (in which case, we
                # arbitrarily show the main message on the first one).
                if not message.path:
                    set_primary_message(span, info['message'])

        label = span['label']
        # Some spans don't have a label.  These seem to just imply
        # that the main "message" is sufficient, and always seems
        # to happen when the span is_primary.
        #
        # This can also happen for macro expansions.
        #
        # Label with an empty string can happen for messages that have
        # multiple spans (starting in 1.21).
        if label is not None:
            # Display the label for this Span.
            add_additional(span, label, info['level'])
        if span['suggested_replacement']:
            # The "suggested_replacement" contains the code that
            # should replace the span.
            child = add_additional(span, None, 'help')
            replacement_template = util.multiline_fix("""
                <div class="rust-links">
                    <a href="replace:%s" class="rust-link">Accept Replacement:</a> %s
                </div>""")
            child.minihtml_text = replacement_template % (
                urllib.parse.urlencode({
                    'id': child.id,
                    'replacement': span['suggested_replacement'],
                }),
                html.escape(span['suggested_replacement'], quote=False),
            )

    # Recurse into children (which typically hold notes).
    for child in info['children']:
        _collect_rust_messages(window, base_path, child, target_path,
                               msg_cb, parent_info.copy(),
                               message)


def _create_cross_links(primary_message):
    """Updates the `links` field of the message for links between the message
    and far-away children.
    """
    def make_file_path(msg):
        if msg.span:
            return 'file:///%s:%s:%s' % (
                msg.path.replace('\\', '/'),
                msg.span[0][0] + 1,
                msg.span[0][1] + 1,
            )
        else:
            # Arbitrarily large line number to force it to the bottom of the
            # file, since we don't know ahead of time how large the file is.
            return 'file:///%s:999999999' % (msg.path,)

    back_link = '<a href="%s">\u2190</a>' % (make_file_path(primary_message),)

    link_template = util.multiline_fix("""
        <div class="rust-links">
            <a href="{url}" class="rust-link">Note: {filename}{lineno}</a>
        </div>""")

    # Determine which children are "far away".
    link_set = set()
    links = []
    for child in primary_message.children:
        child_lineno = child.lineno()
        seen_key = (child.path, child_lineno)
        # Only include a link if it is not close to the main message.
        if child.path != primary_message.path or \
           abs(child_lineno - primary_message.lineno()) > 5:
            if seen_key in link_set:
                continue
            link_set.add(seen_key)
            if child.span:
                lineno = ':%s' % (child_lineno + 1,)
            else:
                # AFAIK, this code path is not possible, but leaving it here
                # to be safe.
                lineno = ''
            if child.path == primary_message.path:
                if child_lineno < primary_message.lineno():
                    filename = '\u2191'  # up arrow
                else:
                    filename = '\u2193'  # down arrow
            else:
                filename = os.path.basename(child.path)
            minihtml_text = link_template.format(
                url=make_file_path(child),
                filename=filename,
                lineno=lineno,
            )
            links.append(minihtml_text)
            child.back_link = back_link

    # Add additional messages with clickable links.
    for link_text in links:
        link = Message()
        link.path = primary_message.path
        link.span = primary_message.span
        link.level = primary_message.level
        link.primary = False
        link.parent = primary_message
        link.minihtml_text = link_text
        primary_message.children.append(link)
