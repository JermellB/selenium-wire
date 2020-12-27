"""This module manages the integraton with mitmproxy."""
import asyncio

try:
    import mitmproxy
except ImportError as e:
    raise ImportError("To use the mitmproxy backend you must first "
                      "install mitmproxy with 'pip install mitmproxy'.") from e

from mitmproxy import addons
from mitmproxy.master import Master
from mitmproxy.options import Options
from mitmproxy.proxy.config import ProxyConfig
from mitmproxy.proxy.server import ProxyServer

from seleniumwire.proxy.handler import ADMIN_PATH, AdminMixin, CaptureMixin
from seleniumwire.proxy.modifier import RequestModifier
from seleniumwire.proxy.request import Request, Response
from seleniumwire.proxy.storage import RequestStorage
from seleniumwire.proxy.utils import get_upstream_proxy

RETRIES = 3
PORT_RANGE_START = 9000
PORT_RANGE_END = 9999

DEFAULT_CONFDIR = '~/.mitmproxy'
DEFAULT_UPSTREAM_CERT = False
DEFAULT_STREAM_WEBSOCKETS = True


class MitmProxyRequestHandler(AdminMixin, CaptureMixin):
    """Mitmproxy add-on which provides request modification and capture."""

    def __init__(self, storage, options):
        self.storage = storage
        self.options = options
        self.scopes = []
        self.modifier = RequestModifier()

    def requestheaders(self, flow):
        # Requests that are being captured or are admin requests are not streamed.
        if self.in_scope(self.scopes, flow.request.url) or flow.request.url.startswith(ADMIN_PATH):
            flow.request.stream = False

    def request(self, flow):
        if flow.request.url.startswith(ADMIN_PATH):
            self.handle_admin(flow)
        else:
            # Make any modifications to the original request
            self.modifier.modify_request(flow.request, bodyattr='raw_content')

            # Convert to one of our requests for handling
            request = self._create_request(flow)

            self.capture_request(request)
            if request.id is not None:  # Will not be None when captured
                flow.request.id = request.id

            # Could possibly use mitmproxy's 'anticomp' option instead of this
            if self.options.get('disable_encoding') is True:
                flow.request.headers['Accept-Encoding'] = 'identity'

    def response(self, flow):
        # Make any modifications to the response
        self.modifier.modify_response(flow.response, flow.request)

        if not hasattr(flow.request, 'id'):
            # Request was not stored
            return

        # Convert the mitmproxy specific response to one of our responses
        # for handling.
        response = Response(
            status_code=flow.response.status_code,
            reason=flow.response.reason,
            headers=dict(flow.response.headers),
            body=flow.response.raw_content
        )

        self.capture_response(flow.request.id, flow.request.url, response)

    def responseheaders(self, flow):
        # Responses that are being captured or are admin responses are not streamed.
        if self.in_scope(self.scopes, flow.request.url) or flow.request.url.startswith(ADMIN_PATH):
            flow.response.stream = False

    def handle_admin(self, flow):
        request = self._create_request(flow)
        response = self.dispatch_admin(request)

        flow.response = mitmproxy.http.HTTPResponse.make(
            status_code=200,
            content=response.body,
            headers=dict((k, str(v).encode('utf-8')) for k, v in response.headers.items())
        )

    def _create_request(self, flow):
        request = Request(
            method=flow.request.method,
            url=flow.request.url,
            headers=dict(flow.request.headers),
            body=flow.request.raw_content
        )

        return request


class MitmProxy:
    """Run and manage a mitmproxy server instance."""

    def __init__(self, host, port, options):
        # Used to stored captured requests
        self.storage = RequestStorage(
            base_dir=options.pop('request_storage_base_dir', None)
        )

        # mitmproxy specific options
        mitmproxy_opts = Options(
            listen_host=host,
            listen_port=port,
        )

        # Create an instance of the mitmproxy server
        self._master = Master(mitmproxy_opts)
        self._master.server = ProxyServer(ProxyConfig(mitmproxy_opts))
        self._master.addons.add(*addons.default_addons())
        self._master.addons.add(MitmProxyRequestHandler(self.storage, options))

        # Update the options now all addons have been added
        mitmproxy_opts.update(
            confdir=DEFAULT_CONFDIR,
            ssl_insecure=options.get('verify_ssl', True),
            upstream_cert=DEFAULT_UPSTREAM_CERT,
            stream_websockets=DEFAULT_STREAM_WEBSOCKETS,
            **self._get_upstream_proxy_args(options),
            # Options that are prefixed mitm_ are passed through to mitmproxy
            **{k[5:]: v for k, v in options.items() if k.startswith('mitm_')}
        )

        self._event_loop = asyncio.get_event_loop()

    def serve(self):
        """Run the server."""
        asyncio.set_event_loop(self._event_loop)
        self._master.run_loop(self._event_loop.run_forever)

    def address(self):
        """Get a tuple of the address and port the mitmproxy server
        is listening on.
        """
        return self._master.server.address

    def shutdown(self):
        """Shutdown the server and perform any cleanup."""
        self._master.shutdown()
        self.storage.cleanup()

    def _get_upstream_proxy_args(self, options):
        proxy_config = get_upstream_proxy(options)
        http_proxy = proxy_config.get('http')
        https_proxy = proxy_config.get('https')
        conf = None

        if http_proxy and https_proxy:
            if http_proxy.hostport != https_proxy.hostport:
                # We only support a single upstream proxy server
                raise ValueError('Cannot specify both http AND https '
                                 'proxy settings with mitmproxy backend')

            conf = https_proxy
        elif http_proxy:
            conf = http_proxy
        elif https_proxy:
            conf = https_proxy

        args = {}

        if conf:
            scheme, username, password, hostport = conf

            args['mode'] = 'upstream:{}://{}'.format(scheme, hostport)

            if username and password:
                args['upstream_auth'] = '{}:{}'.format(username, password)

        return args
