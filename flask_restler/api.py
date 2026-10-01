from __future__ import absolute_import

from flask import Blueprint, jsonify, request, render_template, json, Response
import os
import warnings
from inspect import isclass
from urllib.parse import urlencode

from . import APIError
from .auth import current_user

from .resource import Resource
from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin
#  from apispec.ext.marshmallow.swagger import schema2jsonschema


DEFAULT = object()
STATIC = os.path.abspath(os.path.join(os.path.dirname(__file__), 'static'))
TEMPLATE = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))


class Api(Blueprint):

    """Implement REST API."""

    def __init__(self, name, import_name, specs=True, version="1", url_prefix=None, specs_url_prefix=None,
                 openapi_ver=None, **kwargs):
        self.version = version
        self.specs = specs
        self.openapi_ver = openapi_ver or '3.0.2'
        self.specs_url_prefix = specs_url_prefix or ''

        if not url_prefix and version:
            url_prefix = "/%s" % version

        if self.specs:
            kwargs['static_folder'] = STATIC
            kwargs['template_folder'] = TEMPLATE

        super(Api, self).__init__(name, import_name, url_prefix=url_prefix, **kwargs)
        self.app = None
        self.registration_options = {}
        self.resources = []

    def register(self, app, options=None):
        """Register self to application."""
        app.errorhandler(APIError)(self.handle_error)
        # NB: self.app stays None here so specs routes added below defer into the blueprint
        # and are flushed by super().register(). Flask 2.x forbids re-registering a blueprint,
        # so we must not trigger route()'s live re-registration during our own registration.
        if self.specs:
            # url_detail=None: these are singleton doc views, not resources. Without it, route()
            # also registers a `/<name>` detail rule (e.g. `/<specs_html>`) that greedily matches
            # any single-segment GET under the blueprint and shadows real endpoints on Werkzeug 2.x.
            self.route('/_specs', url_detail=None, params=dict(authorize=anonimous, update_specs=anonimous))(
                self.specs_view)

            @self.route('/', url_detail=None, params=dict(authorize=anonimous, update_specs=anonimous))
            def specs_html(*args, **kwargs): # noqa
                return Response(render_template('swagger.html'))

        options = options or {}
        result = super(Api, self).register(app, options)
        self.app = app
        # Kept so a route added after registration is built with the prefix and subdomain the
        # blueprint was actually registered under, not just the ones it was constructed with.
        self.registration_options = options
        return result

    def authorize(self, *args, **kwargs):
        """Make authorization process.

        The logic could be redifined for each resource.
        """
        return current_user

    def authorization(self, callback):
        """A decorator which helps to update authorize method for current Api."""
        self.authorize = callback
        return callback

    @staticmethod
    def handle_error(error):
        response = jsonify(error.to_dict())
        response.status_code = error.status_code
        return response

    def connect(self, *args, **kwargs):
        warnings.warn('The @connect method is depricated, use @route instead.')
        return self.route(*args, **kwargs)

    def route(self, resource=None, url=None, url_detail=DEFAULT, params=None, **options):
        """Connect resource to the API."""

        api = self

        def wrapper(res):

            if not isclass(res):
                res = Resource.from_func(res, methods=options.get('methods'), **(params or {}))

            elif not issubclass(res, Resource):
                raise ValueError('Resource should be subclass of api.Resource.')

            api.resources.append(res)

            # Before the blueprint is registered, rules defer into it as usual. Afterwards every
            # blueprint setup method is closed off -- a warning on Flask 2.2, an AssertionError from
            # Blueprint._check_setup_finished on Flask 3 -- so the rules go straight onto the live app
            # through a setup state instead. That is what the deferred closures would have called
            # anyway, so both paths apply the same prefix, subdomain and url defaults.
            add_url_rule = api.add_url_rule
            if api.app is not None:
                state = api.make_setup_state(api.app, api.registration_options, False)
                add_url_rule = state.add_url_rule

            url_ = res.meta.url = url or res.meta.url or ('/%s' % res.meta.name)
            view_func = res.as_view(res.meta.name, api)
            add_url_rule(url_, view_func=view_func, **options)

            for _, (route_, endpoint_, options_) in res.meta.endpoints.values():
                add_url_rule('%s/%s' % (url_, route_.strip('/')), view_func=view_func,
                             defaults={'endpoint': endpoint_}, **options_)

            url_detail_ = url_detail
            if url_detail is DEFAULT:
                url_detail_ = res.meta.url_detail = res.meta.url_detail or \
                    ('%s/<%s>' % (url_, res.meta.name))

            if url_detail:
                add_url_rule(url_detail_, view_func=view_func, **options)

            return res

        if resource is not None and isinstance(resource, type) and issubclass(resource, Resource):
            return wrapper(resource)

        elif isinstance(resource, str):
            url = resource

        return wrapper

    def run(self, Resource, path=None, query_string=None, kwargs=None, **rkwargs):
        """Run given resource manually.

        See werkzeug.test.EnvironBuilder for rkwargs.
        """
        resource = Resource(self, raw=True)

        if isinstance(query_string, dict):
            if 'where' in query_string:
                query_string['where'] = json.dumps(query_string['where'])
            query_string = urlencode(query_string)

        rkwargs['query_string'] = query_string
        args = []
        if path:
            args.append(path)
        ctx = self.app.test_request_context(*args, **rkwargs)

        with ctx:
            kwargs = kwargs or {}
            return resource.dispatch_request(**kwargs)

    def specs_view(self, *args, **kwargs):
        specs = APISpec(title=self.name, version=self.version,
                        openapi_version=self.openapi_ver,
                        servers=[{'url': request.host.rstrip('/') + self.specs_url_prefix + self.url_prefix}],
                        plugins=[MarshmallowPlugin()])

        for resource in self.resources:
            resource.update_specs(specs)

        specs = specs.to_dict()
        if isinstance(self.specs, dict):
            # half-deep merge
            for k, v in self.specs.items():
                if k in specs:
                    specs[k].update(v)
                else:
                    specs[k] = v
        return specs


def url_flask_to_swagger(source):
    """Convert Flask URL to swagger path."""
    return source.replace('<', '{').replace('>', '}')


@staticmethod
def anonimous(*args, **kwargs):
    return True
