import asyncio
import logging
import types

from asgiref.sync import async_to_sync, iscoroutinefunction, sync_to_async

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.core.signals import request_finished
from django.db import connections, transaction
from django.urls import get_resolver, set_urlconf
from django.utils.log import log_response
from django.utils.module_loading import import_string

from .exception import convert_exception_to_response

logger = logging.getLogger("django.request")


class BaseHandler:
    """
    BaseHandler类作为处理程序的基础，定义了处理请求和响应的中间件属性。
    这些中间件用于在请求处理的不同阶段插入自定义逻辑，例如视图处理、模板响应处理和异常处理。
    """
    # 视图中间件，用于在视图函数执行前后插入自定义逻辑
    _view_middleware = None
    # 模板响应中间件，用于在模板响应生成前后插入自定义逻辑
    _template_response_middleware = None
    # 异常中间件，用于在异常处理过程中插入自定义逻辑
    _exception_middleware = None
    # 中间件链，用于按顺序执行多个中间件的逻辑
    _middleware_chain = None

    def load_middleware(self, is_async=False):
        """
        Populate middleware lists from settings.MIDDLEWARE.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._view_middleware = []
        self._template_response_middleware = []
        self._exception_middleware = []

        # 根据是否为异步请求，选择合适的响应获取方法，注意着仅仅只是一个方法，还没有被执行呢
        get_response = self._get_response_async if is_async else self._get_response

        # 将get_response函数包装为异常处理函数，以便在发生异常时能返回合适的响应，这个handler就是用于处理request对象的方法地址
        handler = convert_exception_to_response(get_response)

        # 记录handler是否为异步函数，用于后续流程中决定调用方式
        handler_is_async = is_async

        # 这一个操作是将中间件当成是一个装饰器，轮训的给handler进行装饰
        # 主要思想是将handler转为一个带有___call__()方法的中间件对象，所以执行方式和原handler一样。
        # 又因为，__call__() 方法里面是
        #   -先调用process_request方法，
        #   -然后再调用get_response(),实际上是又进入到了一个__call__,又再次执行process_request()方法，就像递归那样，
        #   -直到找到遇到最初的没有被包装handler，然后执行get_response()时，就不会进如到__call__()方法中，此时返回response对象
        #   -然后执行process_response()方法
        # 所以这就造成了最先包装handler的中间件中的，process_request()方法最先执行，而process_response()方法最后执行。

        for middleware_path in reversed(settings.MIDDLEWARE):
            middleware = import_string(middleware_path)
            middleware_can_sync = getattr(middleware, "sync_capable", True)
            middleware_can_async = getattr(middleware, "async_capable", False)
            if not middleware_can_sync and not middleware_can_async:
                raise RuntimeError(
                    "Middleware %s must have at least one of "
                    "sync_capable/async_capable set to True." % middleware_path
                )
            elif not handler_is_async and middleware_can_sync:
                middleware_is_async = False
            else:
                middleware_is_async = middleware_can_async
            try:
                # Adapt handler, if needed.
                adapted_handler = self.adapt_method_mode(
                    middleware_is_async,
                    handler,
                    handler_is_async,
                    debug=settings.DEBUG,
                    name="middleware %s" % middleware_path,
                )
                # 将handler传递给中间件，并获取一个实例对象，
                mw_instance = middleware(adapted_handler)
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if str(exc):
                        logger.debug("MiddlewareNotUsed(%r): %s", middleware_path, exc)
                    else:
                        logger.debug("MiddlewareNotUsed: %r", middleware_path)
                continue
            else:
                handler = adapted_handler

            if mw_instance is None:
                raise ImproperlyConfigured(
                    "Middleware factory %s returned None." % middleware_path
                )

            if hasattr(mw_instance, "process_view"):
                self._view_middleware.insert(
                    0,
                    self.adapt_method_mode(is_async, mw_instance.process_view),
                )
            if hasattr(mw_instance, "process_template_response"):
                self._template_response_middleware.append(
                    self.adapt_method_mode(
                        is_async, mw_instance.process_template_response
                    ),
                )
            if hasattr(mw_instance, "process_exception"):
                # The exception-handling stack is still always synchronous for
                # now, so adapt that way.
                self._exception_middleware.append(
                    self.adapt_method_mode(False, mw_instance.process_exception),
                )

            # 将mw_instance中间件实例对象，转为handler方法，用于处理request对象。
            # mw_instnce中定义了__call__(),所以类可以当成方法使用
            handler = convert_exception_to_response(mw_instance)
            handler_is_async = middleware_is_async

        # Adapt the top of the stack, if needed.
        handler = self.adapt_method_mode(is_async, handler, handler_is_async)
        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.

        # 所以_middleware_chain 就是handler
        self._middleware_chain = handler

    def adapt_method_mode(
        self,
        is_async,
        method,
        method_is_async=None,
        debug=False,
        name=None,
    ):
        """
        Adapt a method to be in the correct "mode":
        - If is_async is False:
          - Synchronous methods are left alone
          - Asynchronous methods are wrapped with async_to_sync
        - If is_async is True:
          - Synchronous methods are wrapped with sync_to_async()
          - Asynchronous methods are left alone
        """
        if method_is_async is None:
            method_is_async = iscoroutinefunction(method)
        if debug and not name:
            name = name or "method %s()" % method.__qualname__
        if is_async:
            if not method_is_async:
                if debug:
                    logger.debug("Synchronous handler adapted for %s.", name)
                return sync_to_async(method, thread_sensitive=True)
        elif method_is_async:
            if debug:
                logger.debug("Asynchronous handler adapted for %s.", name)
            return async_to_sync(method)
        return method

    def get_response(self, request):
        """
        根据给定的HttpRequest返回一个HttpResponse对象。
        
        该方法负责处理请求并返回相应的响应。它首先为当前线程设置默认的URL解析器，
        然后通过中间件链处理请求，最后检查响应状态码是否大于等于400，如果是，
        则记录响应的详细信息。
        
        参数:
        - request: HttpRequest对象，表示客户端的请求。
        
        返回:
        - HttpResponse对象，表示服务器对请求的响应。
        """
        # 设置当前线程的默认URL解析器
        set_urlconf(settings.ROOT_URLCONF)
        # 通过中间件链处理请求并获取响应，其中包括了视图函数的处理，因为视图函数被包装在了中间链中，
        # 所以执行中间件的过程中，视图函数就被执行了
        # 这里的_middleware_chain 就是函数对象，类似于是一个视图函数，只不过它处理的是所有的request请求
        response = self._middleware_chain(request)
        # 将请求的关闭方法添加到响应的资源关闭列表中
        response._resource_closers.append(request.close)
        # 如果响应状态码大于等于400，则记录响应信息
        if response.status_code >= 400:
            # 记录响应信息
            log_response(
                "%s: %s",
                response.reason_phrase,
                request.path,
                response=response,
                request=request,
            )
        # 返回处理后的响应
        return response

    async def get_response_async(self, request):
        """
        Asynchronous version of get_response.

        Funneling everything, including WSGI, into a single async
        get_response() is too slow. Avoid the context switch by using
        a separate async response path.
        """
        # Setup default url resolver for this thread.
        set_urlconf(settings.ROOT_URLCONF)
        response = await self._middleware_chain(request)
        response._resource_closers.append(request.close)
        if response.status_code >= 400:
            await sync_to_async(log_response, thread_sensitive=False)(
                "%s: %s",
                response.reason_phrase,
                request.path,
                response=response,
                request=request,
            )
        return response


    def _get_response(self, request):
        """
        解析并调用视图，然后分别应用视图、异常以及模板响应中间件。
        这个方法涵盖了请求/响应中间件内部发生的所有操作。

        参数:
        - request: HTTP请求对象

        返回:
        - response: HTTP响应对象
        """
        # 初始化响应对象
        response = None
        # 解析请求以获取视图函数及其参数
        callback, callback_args, callback_kwargs = self.resolve_request(request)

        # 应用视图中间件
        for middleware_method in self._view_middleware:
            # 中间件方法可能返回一个响应对象，如果返回，则结束循环
            response = middleware_method(
                request, callback, callback_args, callback_kwargs
            )
            if response:
                break

        # 如果中间件没有返回响应，调用实际的视图函数
        if response is None:
            # 将视图函数包装为原子操作
            wrapped_callback = self.make_view_atomic(callback)
            # 如果是异步视图，用同步方式运行
            if iscoroutinefunction(wrapped_callback):
                wrapped_callback = async_to_sync(wrapped_callback)
            try:
                # 调用视图函数并获取响应
                response = wrapped_callback(request, *callback_args, **callback_kwargs)
            except Exception as e:
                # 应用异常中间件处理异常
                response = self.process_exception_by_middleware(e, request)
                if response is None:
                    # 如果异常没有被处理，重新抛出
                    raise

        # 检查视图返回的响应是否有效
        self.check_response(response, callback)

        # 如果响应支持延迟渲染，应用模板响应中间件并渲染响应
        if hasattr(response, "render") and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = middleware_method(request, response)
                # 检查中间件返回的响应是否有效
                self.check_response(
                    response,
                    middleware_method,
                    name="%s.process_template_response"
                    % (middleware_method.__self__.__class__.__name__,),
                )
            try:
                # 渲染模板并获取最终响应
                response = response.render()
            except Exception as e:
                # 处理渲染过程中可能抛出的异常
                response = self.process_exception_by_middleware(e, request)
                if response is None:
                    # 如果异常没有被处理，重新抛出
                    raise

        # 返回最终的响应对象
        return response
    async def _get_response_async(self, request):
        """
        Resolve and call the view, then apply view, exception, and
        template_response middleware. This method is everything that happens
        inside the request/response middleware.
        """
        response = None
        callback, callback_args, callback_kwargs = self.resolve_request(request)

        # Apply view middleware.
        for middleware_method in self._view_middleware:
            response = await middleware_method(
                request, callback, callback_args, callback_kwargs
            )
            if response:
                break

        if response is None:
            wrapped_callback = self.make_view_atomic(callback)
            # If it is a synchronous view, run it in a subthread
            if not iscoroutinefunction(wrapped_callback):
                wrapped_callback = sync_to_async(
                    wrapped_callback, thread_sensitive=True
                )
            try:
                response = await wrapped_callback(
                    request, *callback_args, **callback_kwargs
                )
            except Exception as e:
                response = await sync_to_async(
                    self.process_exception_by_middleware,
                    thread_sensitive=True,
                )(e, request)
                if response is None:
                    raise

        # Complain if the view returned None or an uncalled coroutine.
        self.check_response(response, callback)

        # If the response supports deferred rendering, apply template
        # response middleware and then render the response
        if hasattr(response, "render") and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = await middleware_method(request, response)
                # Complain if the template response middleware returned None or
                # an uncalled coroutine.
                self.check_response(
                    response,
                    middleware_method,
                    name="%s.process_template_response"
                    % (middleware_method.__self__.__class__.__name__,),
                )
            try:
                if iscoroutinefunction(response.render):
                    response = await response.render()
                else:
                    response = await sync_to_async(
                        response.render, thread_sensitive=True
                    )()
            except Exception as e:
                response = await sync_to_async(
                    self.process_exception_by_middleware,
                    thread_sensitive=True,
                )(e, request)
                if response is None:
                    raise

        # Make sure the response is not a coroutine
        if asyncio.iscoroutine(response):
            raise RuntimeError("Response is still a coroutine.")
        return response

    def resolve_request(self, request):
        """
        解析请求并设置URL配置。返回解析后的视图，包括其参数和关键字参数。

        参数:
        - request: 请求对象，可能包含urlconf属性用于指定自定义的URL配置。

        返回:
        - resolver_match: 解析后的视图及其参数和关键字参数。
        """
        # 根据请求对象解析URL配置。
        if hasattr(request, "urlconf"):
            urlconf = request.urlconf
            set_urlconf(urlconf)
            resolver = get_resolver(urlconf)
        else:
            resolver = get_resolver()
        # 使用解析的URL配置来解析视图，并将解析结果赋值给请求对象。
        resolver_match = resolver.resolve(request.path_info)
        request.resolver_match = resolver_match
        return resolver_match


    def check_response(self, response, callback, name=None):
        """
        Raise an error if the view returned None or an uncalled coroutine.
        """
        if not (response is None or asyncio.iscoroutine(response)):
            return
        if not name:
            if isinstance(callback, types.FunctionType):  # FBV
                name = "The view %s.%s" % (callback.__module__, callback.__name__)
            else:  # CBV
                name = "The view %s.%s.__call__" % (
                    callback.__module__,
                    callback.__class__.__name__,
                )
        if response is None:
            raise ValueError(
                "%s didn't return an HttpResponse object. It returned None "
                "instead." % name
            )
        elif asyncio.iscoroutine(response):
            raise ValueError(
                "%s didn't return an HttpResponse object. It returned an "
                "unawaited coroutine instead. You may need to add an 'await' "
                "into your view." % name
            )

    # Other utility methods.

    def make_view_atomic(self, view):
        non_atomic_requests = getattr(view, "_non_atomic_requests", set())
        for alias, settings_dict in connections.settings.items():
            if settings_dict["ATOMIC_REQUESTS"] and alias not in non_atomic_requests:
                if iscoroutinefunction(view):
                    raise RuntimeError(
                        "You cannot use ATOMIC_REQUESTS with async views."
                    )
                view = transaction.atomic(using=alias)(view)
        return view

    def process_exception_by_middleware(self, exception, request):
        """
        Pass the exception to the exception middleware. If no middleware
        return a response for this exception, return None.
        """
        for middleware_method in self._exception_middleware:
            response = middleware_method(request, exception)
            if response:
                return response
        return None


def reset_urlconf(sender, **kwargs):
    """Reset the URLconf after each request is finished."""
    set_urlconf(None)


request_finished.connect(reset_urlconf)
