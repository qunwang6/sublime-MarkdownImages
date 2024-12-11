import sublime
import sublime_plugin
from collections import defaultdict
import struct
import imghdr
import base64
import urllib.request
import urllib.parse
import io
import os.path
import subprocess
import sys
import re


DEBUG = False

def debug(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

settings_file = 'MarkdownImages.sublime-settings'


def get_settings():
    return sublime.load_settings(settings_file)


class MarkdownImagesPlugin(sublime_plugin.EventListener):

    def on_load(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_load', False)
        show_remote = settings.get('show_remote_images_on_load', False)
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_post_save(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_post_save')
        show_remote = settings.get('show_remote_images_on_post_save')
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_close(self, view):
        ImageHandler.on_close(view)

    def _should_run_for_extension(self, settings, view):
        extensions = settings.get('extensions')
        fn = view.file_name()
        _, ext = os.path.splitext(fn)
        # extensions can be either a list or single string
        if isinstance(extensions, str):
            return ext == extensions
        return ext in extensions

    def _update_images(self, settings, view, **kwargs):
        max_width = settings.get('img_maxwidth', None)
        base_path = settings.get('base_path', None)
        arg_trim = settings.get('remove_local_url_arguments', None)
        base_zoom = settings.get('base_zoom', None)
        ImageHandler.hide_images(view)
        ImageHandler.show_images(view,
                                 max_width=max_width,
                                 show_local=kwargs.get('show_local', False),
                                 show_remote=kwargs.get('show_remote', False),
                                 base_path=base_path,
                                 arg_trim=arg_trim,
                                 base_zoom=base_zoom)


class ImageHandler:
    """
    Static class to bundle image handling.
    """

    selector = 'markup.underline.link.image.markdown'
    # Maps view IDs to sets of phantom (key, html) pairs
    phantoms = defaultdict(set)
    # Cached remote URL image data. Kept even if not rendered.
    urldata = defaultdict(dict)

    @staticmethod
    def on_close(view):
        ImageHandler._erase_phantoms(view)
        ImageHandler.urldata.pop(view.id(), None)

    @staticmethod
    def show_images(view, max_width=None, show_local=True, show_remote=False, base_path="", arg_trim=False, base_zoom=1.0):
        debug("show_images")
        if not show_local and not show_remote:
            debug("doing nothing")
            return
        # Note: Excessive size will cause the ST3 window to become blank
        # unrecoverably. 1024 apperas to be a safe limit,
        # but can possibly go higher.
        if not max_width or max_width < 0:
            max_width = 1024
        if base_zoom <= 0:
            base_zoom = 1.0

        skip = 0

        phantoms = {}
        img_regs = view.find_by_selector(ImageHandler.selector)
        # Handling space characters in image links
        # Image links not enclosed in <> that contain spaces
        # are parsed by sublime as multiple links instead of one.
        # Example: "![](my file.png)" gets parsed as two links: "my" and "file.png".
        # We detect when two links are separated only by spaces and merge them
        indexes_to_merge = []
        for i, (left_reg, right_reg) in enumerate(zip(img_regs, img_regs[1:])):
            inter_region = sublime.Region(left_reg.end(), right_reg.begin())
            if (view.substr(inter_region)).isspace():
                # the inter_region is all spaces
                # Noting that left and right regions must be merged
                indexes_to_merge += [i+1]
        new_img_regs = []
        for i in range(len(img_regs)):
            if i in indexes_to_merge:
                new_img_regs[-1] = new_img_regs[-1].cover(img_regs[i])
            else:
                new_img_regs += [img_regs[i]]
        img_regs = new_img_regs

        for region in reversed(img_regs):
            ttype = None
            urldata = None
            rel_p = view.substr(region)

            # If an image link is enclosed in <> to tolerate spaces in it,
            # then the > appears at the end of rel_p for some reason.
            # This character makes the link invalid, so it must be removed
            if rel_p[-1] == '>':
                rel_p = rel_p[0:-1]
            
            # (Windows) cutting the drive letter from the path,
            # otherwise urlparse interprets it as a scheme (like 'file' or 'http')
            # and generates a bogus url object like:
            # url= ParseResult(scheme='c', netloc='', path='/path/image.png', params='', query='', fragment='')
            drive_letter, rel_p = os.path.splitdrive(rel_p)

            url = urllib.parse.urlparse(rel_p)
            if url.scheme and url.scheme != 'file':
                if not show_remote:
                    continue

                # We can't render SVG images, so skip the request
                # Note: not all URLs that return SVG end with .svg
                # We could use a HEAD request to check the Content-Type before
                # downloading the image, but the size of an SVG is typically
                # so small to not be worth the extra request
                if url.path.endswith('.svg'):
                    continue

                # Add WebP support
                if url.path.endswith('.webp'):
                    debug("detected WebP image url", rel_p)

                debug("image url", rel_p)
                data = ImageHandler.urldata[view.id()].get(rel_p)
                if not data:
                    try:
                        data = urllib.request.urlopen(rel_p)
                    except Exception as e:
                        debug("Failed to open URL {}:".format(rel_p), e)
                        continue

                    try:
                        data = data.read()
                    except Exception as e:
                        debug("Failed to read data from URL {}:".format(rel_p), e)
                        continue

                try:
                    w, h, ttype = get_image_size(io.BytesIO(data))
                except Exception as e:
                    msg = "Failed to get_image_size for data from URL {}"
                    debug(msg.format(rel_p), e)
                    continue

                FMT = u'''
                    <a href="{}">
                        <img src="data:image/{}" class="centerImage" {}>
                    </a>
                '''
                b64_data = base64.encodestring(data).decode('ascii')
                b64_data = b64_data.replace('\n', '')

                img = "{};base64,{}".format(ttype, b64_data)
                urldata = data
            else:
                if not show_local:
                    continue

                # Convert relative paths to be relative to the current file
                # or project folder.
                # NOTE: if the current file or project folder can't be
                # determined (e.g. if the view content is not in a project and
                # hasn't been saved), then it will anchor to /.
                path = url.path

                # Un-escape URL-encoded sequences (like "%20" for space).
                path = urllib.parse.unquote(path)

                # Force paths to be prefixed with base_path if it was provided
                # in settings.
                if base_path:
                    path = os.path.join(base_path, path)
                if not os.path.isabs(path):
                    folder = get_path_for(view)
                    path = os.path.join(folder, path)
                path = os.path.normpath(path)
                # (Windows) Adding back the drive letter that was cut from the path before
                path = drive_letter + path

                url = url._replace(scheme='file', path=path)

                FMT = '''
                    <a href="{}">
                        <img src="{}" class="centerImage" {}>
                    </a>
                '''
                try:
                    w, h, ttype = get_file_image_size(path)
                except Exception as e:
                    debug("Failed to load {}:".format(path), e)
                    continue
                img = urllib.parse.urlunparse(url)

                # Removes arguments in the URL
                if arg_trim:
                    img = img.split('?')[0]
                    debug("split")

                # On Windows, urlunparse adds a third slash after 'file://' for some reason
                # This breaks the image url, so it must be removed
                # splitdrive() detects windows because it only returns something if the
                # path contains a drive letter
                if os.path.splitdrive(path)[0]:
                    img = img.replace('file:///', 'file://', 1)

            if not ttype:
                debug("unknown ttype")
                continue

            # Handle both jpeg and jpg extensions
            if ttype == "jpeg":
                debug("handling jpeg/jpg image")
                if img.lower().endswith('.jpg'):
                    ttype = "jpg"

            line_region = view.line(region)
            zoom, width, height, imgattr = check_imgattr(view, line_region, region)

            w *= base_zoom
            h *= base_zoom

            # Dimensions (and zoom) defined in custom size should override max_width
            if zoom is not None:
                w *= zoom
                h *= zoom
            elif width is not None and height is not None:
                w = width
                h = height
            elif width is not None:
                m = width / w
                w = width
                h *= m
            elif height is not None:
                m = height / h
                h = height
                w *= m
            elif max_width and w > max_width:
                m = max_width / w
                w = max_width
                h *= m

            if imgattr:
                imgattr += ' '
            imgattr += 'width="{}" height="{}"'.format(w, h)

            # Force the phantom image view to append past the end of the line
            # Otherwise, the phantom image view interlaces in between
            # word-wrapped lines
            line_region.a = line_region.b

            debug("region", region)
            debug("line_region", line_region)

            key = 'mdimage-' + str(line_region.b)
            html_img = FMT.format(url.geturl(), img, imgattr)

            phantom = (key, html_img)
            phantoms[phantom[0]] = phantom
            if phantom in ImageHandler.phantoms[view.id()]:
                debug("Phantom unchanged")
                continue

            debug("Creating phantom", phantom[0])
            view.add_phantom(phantom[0],
                             sublime.Region(line_region.b),
                             phantom[1],
                             sublime.LAYOUT_BLOCK,
                             ImageHandler.on_navigate)
            ImageHandler.phantoms[view.id()].add(phantom)
            if urldata is not None:
                ImageHandler.urldata[view.id()][rel_p] = urldata

        # Erase leftover phantoms
        for p in list(ImageHandler.phantoms[view.id()]):
            if phantoms.get(p[0]) != p:
                view.erase_phantoms(p[0])
                ImageHandler.phantoms[view.id()].remove(p)

        if not ImageHandler.phantoms[view.id()]:
            ImageHandler.phantoms.pop(view.id(), None)

    @staticmethod
    def on_navigate(url):
        print("MarkdownImages: Opening URL/path [%s]" % url)

        if sys.platform == "darwin": # MacOS
            subprocess.call(["open", url])

        elif sys.platform in ["linux", "linux2"]:
            subprocess.call(["xdg-open", url])

        elif sys.platform == "win32": # Windows
            if url.startswith("file:///"):
                # Python provides a shorthand method for local files on Windows.
                os.startfile(url)

            else:
                subprocess.call(["start", url], shell=True)

    @staticmethod
    def hide_images(view):
        ImageHandler._erase_phantoms(view)

    @staticmethod
    def _erase_phantoms(view):
        for p in ImageHandler.phantoms[view.id()]:
            view.erase_phantoms(p[0])
        ImageHandler.phantoms.pop(view.id(), None)
        # Cached URL data is kept

def check_imgattr(view, line_region, link_region=None):
    # find attrs for this link
    full_line = view.substr(line_region)
    link_till_eol = full_line[link_region.a - line_region.a:]
    # find attr if present
    m = re.match(r'.*\)\{(.*)\}', link_till_eol)
    imgattr = m.groups()[0] if m else ''

    width, imgattr = cut_attr(imgattr, 'width')
    height, imgattr = cut_attr(imgattr, 'height')
    zoom, imgattr = cut_attr(imgattr, 'zoom')

    return zoom, width, height, imgattr

def cut_attr(imgattr, attr):
    m = re.search(attr+r'\s*=\s*\"?(\d+(\.\d+)?)\"?', imgattr)
    if m:
        try:
            val = float(m.groups()[0])
            imgattr = ' '.join(s for s in [imgattr[:m.start()].rstrip(), imgattr[m.end():].lstrip()] if s)
            return val, imgattr
        except ValueError as e:
            debug("failed parsing float value from '" + m.groups()[0] + "'", e)
    return None, imgattr

def get_file_image_size(img):
    with open(img, 'rb') as f:
        return get_image_size(f)


def get_image_size(f):
    """
    Determine the image type of img and return its size.
    """
    head = f.read(24)
    ttype = None

    debug(str(head))
    debug(str(head[:4]))
    debug(head[:4] == b'<svg')

    if imghdr.what('', head) == 'png':
        debug('detected png')
        ttype = "png"
        check = struct.unpack('>i', head[4:8])[0]
        if check != 0x0d0a1a0a:
            return None, None, ttype
        width, height = struct.unpack('>ii', head[16:24])
    elif imghdr.what('', head) == 'gif':
        debug('detected gif')
        ttype = "gif"
        width, height = struct.unpack('<HH', head[6:10])
    elif imghdr.what('', head) == 'jpeg':
        debug('detected jpeg')
        ttype = "jpeg"  # 添加 ttype 设置
        try:
            f.seek(0)  # Read 0xff next
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf:
                f.seek(size, 1)
                byte = f.read(1)
                while ord(byte) == 0xff:
                    byte = f.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', f.read(2))[0] - 2
            # SOFn block
            f.seek(1, 1)  # skip precision byte.
            height, width = struct.unpack('>HH', f.read(4))
        except Exception as e:
            debug("determining jpeg image size failed", e)
            return None, None, ttype
    elif head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        debug('detected webp')
        ttype = "webp"
        head += f.read(7)
        if head[12:16] == b"VP8 ":
            width, height = struct.unpack("<HH", head[26:30])
        elif head[12:16] == b"VP8X":
            width = struct.unpack("<I", head[24:27] + b"\0")[0]
            height = struct.unpack("<I", head[27:30] + b"\0")[0]
        elif head[12:16] == b"VP8L":
            b = head[21:25]
            width = (((b[1] & 63) << 8) | b[0]) + 1
            height = (((b[3] & 15) << 10) | (b[2] << 2) | ((b[1] & 192) >> 6)) + 1
        else:
            debug('unable to detect WebP key frame')
            return None, None, None
    elif head[:4] == b'<svg':
        debug('detected svg')
        # SVG is not rendered by ST3 in phantoms.
        # The SVG would need to be rendered as png/jpg separately, and its data
        # placed into the phantom
        return None, None, None
    else:
        debug('unable to detect image')
        return None, None, None
    return width, height, ttype


def get_path_for(view):
    """
    Returns the path of the current file in view.
    Returns / if no path is found
    """
    if view.file_name():
        return os.path.dirname(view.file_name())
    if view.window().project_file_name():
        return os.path.dirname(view.window().project_file_name())
    return '/'


class MarkdownImagesShowCommand(sublime_plugin.TextCommand):
    """
    Show local images inline.
    """

    def run(self, edit, **kwargs):
        settings = get_settings()
        max_width = settings.get('img_maxwidth', None)
        show_local = kwargs.get('show_local', True)
        show_remote = kwargs.get('show_remote', False)
        base_path = settings.get('base_path', None)
        arg_trim = settings.get('remove_local_url_arguments', None)
        base_zoom = settings.get('base_zoom', None)
        ImageHandler.show_images(self.view,
                                 show_local=show_local,
                                 show_remote=show_remote,
                                 max_width=max_width,
                                 base_path=base_path,
                                 arg_trim=arg_trim,
                                 base_zoom=base_zoom)


class MarkdownImagesHideCommand(sublime_plugin.TextCommand):
    """
    Hide all shown images.
    """

    def run(self, edit):
        ImageHandler.hide_images(self.view)
