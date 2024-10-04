from io import IOBase

from django.conf import settings
from django.core import signals
from django.core.handlers import base
from django.http import HttpRequest, QueryDict, parse_cookie
from django.urls import set_script_prefix
from django.utils.encoding import repercent_broken_unicode
from django.utils.functional import cached_property
from django.utils.regex_helper import _lazy_re_compile

_slashes_re = _lazy_re_compile(rb"/+")


class LimitedStream(IOBase):
    """
    Wrap another stream to disallow reading it past a number of bytes.

    Based on the implementation from werkzeug.wsgi.LimitedStream
    See https://github.com/pallets/werkzeug/blob/dbf78f67/src/werkzeug/wsgi.py#L828
    """

    def __init__(self, stream, limit):
        self._read = stream.read
        self._readline = stream.readline
        self._pos = 0
        self.limit = limit

    def read(self, size=-1, /):
        _pos = self._pos
        limit = self.limit
        if _pos >= limit:
            return b""
        if size == -1 or size is None:
            size = limit - _pos
        else:
            size = min(size, limit - _pos)
        data = self._read(size)
        self._pos += len(data)
        return data

    def readline(self, size=-1, /):
        _pos = self._pos
        limit = self.limit
        if _pos >= limit:
            return b""
        if size == -1 or size is None:
            size = limit - _pos
        else:
            size = min(size, limit - _pos)
        line = self._readline(size)
        self._pos += len(line)
        return line


class WSGIRequest(HttpRequest):
    def __init__(self, environ):
        script_name = get_script_name(environ)
        # If PATH_INFO is empty (e.g. accessing the SCRIPT_NAME URL without a
        # trailing slash), operate as if '/' was requested.
        path_info = get_path_info(environ) or "/"
        self.environ = environ
        self.path_info = path_info
        # be careful to only replace the first slash in the path because of
        # http://test/something and http://test//something being different as
        # stated in RFC 3986.
        self.path = "%s/%s" % (script_name.rstrip("/"), path_info.replace("/", "", 1))
        self.META = environ
        self.META["PATH_INFO"] = path_info
        self.META["SCRIPT_NAME"] = script_name
        self.method = environ["REQUEST_METHOD"].upper()
        # Set content_type, content_params, and encoding.
        self._set_content_type_params(environ)
        try:
            content_length = int(environ.get("CONTENT_LENGTH"))
        except (ValueError, TypeError):
            content_length = 0
        self._stream = LimitedStream(self.environ["wsgi.input"], content_length)
        self._read_started = False
        self.resolver_match = None

    def _get_scheme(self):
        return self.environ.get("wsgi.url_scheme")

    @cached_property
    def GET(self):
        # The WSGI spec says 'QUERY_STRING' may be absent.
        raw_query_string = get_bytes_from_wsgi(self.environ, "QUERY_STRING", "")
        return QueryDict(raw_query_string, encoding=self._encoding)

    def _get_post(self):
        if not hasattr(self, "_post"):
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post

    @cached_property
    def COOKIES(self):
        raw_cookie = get_str_from_wsgi(self.environ, "HTTP_COOKIE", "")
        return parse_cookie(raw_cookie)

    @property
    def FILES(self):
        if not hasattr(self, "_files"):
            self._load_post_and_files()
        return self._files

    POST = property(_get_post, _set_post)


class WSGIHandler(base.BaseHandler):
    """
    该函数定义了一个名为WSGIHandler的类，继承自base.BaseHandler。
    该类用于处理WSGI（Web Server Gateway Interface）请求。
    WSGI是一种通用的接口标准，用于在Python中实现Web应用和Web服务器之间的通信。
    该类可能包含了处理HTTP请求、解析请求参数、生成响应等功能。
    """
    request_class = WSGIRequest

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 加载中间件其实就是生产handler方法，也就是get_response方法。
        self.load_middleware()

    def __call__(self, environ, start_response):
        """
        调用该类的实例时执行的函数。
    
        参数:
        - environ: WSGI环境字典，包含请求的所有相关信息。
        - start_response: 一个函数，用于发送HTTP响应的状态和头部。
    
        返回:
        - response: WSGI响应对象，可以是任何可迭代的对象。
        """
        # 设置脚本前缀，用于支持虚拟主机
        set_script_prefix(get_script_name(environ))
        # 发送请求开始的信号，允许其他地方监听请求的开始
        signals.request_started.send(sender=self.__class__, environ=environ)
        # 根据WSGI环境创建请求对象
        request = self.request_class(environ)
        # 处理请求对象response,并获取响应对象response.
        response = self.get_response(request)
    
        # 设置响应对象的处理程序类属性，用于追踪处理程序的类型
        response._handler_class = self.__class__
    
        # 格式化HTTP响应状态行
        status = "%d %s" % (response.status_code, response.reason_phrase)
        # 整合响应头部，包括Cookies
        response_headers = [
            *response.items(),
            *(("Set-Cookie", c.output(header="")) for c in response.cookies.values()),
        ]
        # 开始发送响应，包括状态行和头部
        start_response(status, response_headers)
        # 如果响应中包含要流式传输的文件，并且WSGI环境支持文件包装器
        if getattr(response, "file_to_stream", None) is not None and environ.get(
                "wsgi.file_wrapper"
        ):
            # 如果使用了`wsgi.file_wrapper`，WSGI服务器不会调用响应对象的.close方法，
            # 而是调用文件包装器的.close方法。因此，需要将文件流的.close方法替换为响应对象的.close方法，
            # 以确保所有文件都能被正确关闭。
            response.file_to_stream.close = response.close
            # 使用WSGI环境提供的文件包装器包装文件流，以支持流式响应
            response = environ["wsgi.file_wrapper"](
                response.file_to_stream, response.block_size
            )
        # 返回WSGI响应对象
        return response


def get_path_info(environ):
    """Return the HTTP request's PATH_INFO as a string."""
    path_info = get_bytes_from_wsgi(environ, "PATH_INFO", "/")

    return repercent_broken_unicode(path_info).decode()


def get_script_name(environ):
    """
    Return the equivalent of the HTTP request's SCRIPT_NAME environment
    variable. If Apache mod_rewrite is used, return what would have been
    the script name prior to any rewriting (so it's the script name as seen
    from the client's perspective), unless the FORCE_SCRIPT_NAME setting is
    set (to anything).
    """
    if settings.FORCE_SCRIPT_NAME is not None:
        return settings.FORCE_SCRIPT_NAME

    # If Apache's mod_rewrite had a whack at the URL, Apache set either
    # SCRIPT_URL or REDIRECT_URL to the full resource URL before applying any
    # rewrites. Unfortunately not every web server (lighttpd!) passes this
    # information through all the time, so FORCE_SCRIPT_NAME, above, is still
    # needed.
    script_url = get_bytes_from_wsgi(environ, "SCRIPT_URL", "") or get_bytes_from_wsgi(
        environ, "REDIRECT_URL", ""
    )

    if script_url:
        if b"//" in script_url:
            # mod_wsgi squashes multiple successive slashes in PATH_INFO,
            # do the same with script_url before manipulating paths (#17133).
            script_url = _slashes_re.sub(b"/", script_url)
        path_info = get_bytes_from_wsgi(environ, "PATH_INFO", "")
        script_name = script_url[: -len(path_info)] if path_info else script_url
    else:
        script_name = get_bytes_from_wsgi(environ, "SCRIPT_NAME", "")

    return script_name.decode()


def get_bytes_from_wsgi(environ, key, default):
    """
    Get a value from the WSGI environ dictionary as bytes.

    key and default should be strings.
    """
    value = environ.get(key, default)
    # Non-ASCII values in the WSGI environ are arbitrarily decoded with
    # ISO-8859-1. This is wrong for Django websites where UTF-8 is the default.
    # Re-encode to recover the original bytestring.
    return value.encode("iso-8859-1")


def get_str_from_wsgi(environ, key, default):
    """
    Get a value from the WSGI environ dictionary as str.

    key and default should be str objects.
    """
    value = get_bytes_from_wsgi(environ, key, default)
    return value.decode(errors="replace")
