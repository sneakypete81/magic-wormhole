# NO unicode_literals or static.Data() will break, because it demands
# a str on Python 2
from __future__ import print_function
import os, time, json
from twisted.python import log
from twisted.internet import reactor, endpoints
from twisted.application import service, internet
from twisted.web import server, static, resource
from autobahn.twisted.resource import WebSocketResource
from .endpoint_service import ServerEndpointService
from .. import __version__
from .database import get_db
from .rendezvous import Rendezvous
from .rendezvous_websocket import WebSocketRendezvousFactory
from .transit_server import Transit

SECONDS = 1.0
MINUTE = 60*SECONDS

CHANNEL_EXPIRATION_TIME = 11*MINUTE
EXPIRATION_CHECK_PERIOD = 10*MINUTE

class Root(resource.Resource):
    # child_FOO is a nevow thing, not a twisted.web.resource thing
    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild(b"", static.Data(b"Wormhole Relay\n", "text/plain"))

class PrivacyEnhancedSite(server.Site):
    logRequests = True
    def log(self, request):
        if self.logRequests:
            return server.Site.log(self, request)

class RelayServer(service.MultiService):
    def __init__(self, rendezvous_web_port, transit_port,
                 advertise_version, db_url=":memory:", blur_usage=None,
                 signal_error=None, stats_file=None):
        service.MultiService.__init__(self)
        self._blur_usage = blur_usage

        db = get_db(db_url)
        welcome = {
            # The primary (python CLI) implementation will emit a message if
            # its version does not match this key. If/when we have
            # distributions which include older version, but we still expect
            # them to be compatible, stop sending this key.
            "current_cli_version": __version__,

            # adding .motd will cause all clients to display the message,
            # then keep running normally
            #"motd": "Welcome to the public relay.\nPlease enjoy this service.",

            # adding .error will cause all clients to fail, with this message
            #"error": "This server has been disabled, see URL for details.",
            }
        if advertise_version:
            welcome["current_cli_version"] = advertise_version
        if signal_error:
            welcome["error"] = signal_error

        self._rendezvous = Rendezvous(db, welcome, blur_usage)
        self._rendezvous.setServiceParent(self) # for the pruning timer

        root = Root()
        wsrf = WebSocketRendezvousFactory(None, self._rendezvous)
        root.putChild(b"v1", WebSocketResource(wsrf))

        site = PrivacyEnhancedSite(root)
        if blur_usage:
            site.logRequests = False

        r = endpoints.serverFromString(reactor, rendezvous_web_port)
        rendezvous_web_service = ServerEndpointService(r, site)
        rendezvous_web_service.setServiceParent(self)

        if transit_port:
            transit = Transit(db, blur_usage)
            transit.setServiceParent(self) # for the timer
            t = endpoints.serverFromString(reactor, transit_port)
            transit_service = ServerEndpointService(t, transit)
            transit_service.setServiceParent(self)

        self._stats_file = stats_file
        t = internet.TimerService(EXPIRATION_CHECK_PERIOD, self.timer)
        t.setServiceParent(self)

        # make some things accessible for tests
        self._db = db
        self._root = root
        self._rendezvous_web_service = rendezvous_web_service
        self._rendezvous_websocket = wsrf
        if transit_port:
            self._transit = transit
            self._transit_service = transit_service

    def startService(self):
        service.MultiService.startService(self)
        log.msg("websocket listening on /wormhole-relay/ws")
        log.msg("Wormhole relay server (Rendezvous and Transit) running")
        if self._blur_usage:
            log.msg("blurring access times to %d seconds" % self._blur_usage)
            log.msg("not logging HTTP requests")
        else:
            log.msg("not blurring access times")

    def timer(self):
        now = time.time()
        old = now - CHANNEL_EXPIRATION_TIME
        self._rendezvous.prune_all_apps(now, old)
        self.dump_stats(now, validity=EXPIRATION_CHECK_PERIOD+60)

    def dump_stats(self, now, validity):
        if not self._stats_file:
            return
        tmpfn = self._stats_file + ".tmp"

        data = {}
        data["created"] = now
        data["valid_until"] = now + validity

        data["rendezvous"] = self._rendezvous.get_stats()

        with open(tmpfn, "wb") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        os.rename(tmpfn, self._stats_file)
