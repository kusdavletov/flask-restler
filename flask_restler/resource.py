"""Base API resource."""

from __future__ import absolute_import

import collections
import logging
import math
import re
from typing import Optional

from apispec import yaml_utils
from flask import request, current_app, abort, Response
from flask.json import dumps
from flask.views import View

from . import APIError, logger
from .auth import current_user
from .filters import Filters, FILTERS_ARG
from copy import deepcopy

try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode


PER_PAGE_ARG = 'per_page'
PAGE_ARG = 'page'
SORT_ARG = 'sort'
INTERNAL_ARGS = set([PER_PAGE_ARG, PAGE_ARG, SORT_ARG, FILTERS_ARG])
RE_URL = re.compile(r'<(?:[^:<>]+:)?([^<>]+)>')


class ResourceOptions(object):
    """Prepare resource options."""

    def __init__(self, cls):
        """Initialize resources' options."""
        # Store link to self.meta
        self.meta = meta = getattr(cls, "Meta", None)

        self.cls = cls

        # Inherit meta from parents
        for base in reversed(cls.mro()):
            if not hasattr(base, "Meta"):
                continue

            for k, v in base.Meta.__dict__.items():
                if k.startswith('__'):
                    continue
                setattr(self, k, v)

        # Generate name
        self.name = (meta and getattr(meta, 'name', None)) or \
            cls.__name__.lower().split('resource', 1)[0]

        if self.per_page:  # noqa
            self.per_page = int(self.per_page)

        if self.specs:  # noqa
            self.specs = dict(self.specs)

        if self.strict:  # noqa
            if not isinstance(self.strict, collections.Iterable):
                self.strict = INTERNAL_ARGS
            self.strict = set(self.strict) | INTERNAL_ARGS

        # Setup endpoints
        self.endpoints = getattr(self, 'endpoints', {})
        self.endpoints.update({
            value.route[1]: (value, value.route) for value in cls.__dict__.values()
            if hasattr(value, 'route') and isinstance(value.route, tuple)
        })

        # Setup schema_meta
        self.schema_meta = self.schema_meta or {
            k[7:]: self.__dict__[k] for k in self.__dict__
            if k.startswith('schema_') and not k == 'schema_meta'
        }

        # Setup filters
        self.filters = self.filters_converter(self.filters, cls)

        # Setup sorting
        self.sorting = dict(n if isinstance(n, (list, tuple)) else (n, n) for n in self.sorting)

    def __repr__(self):
        return "<Options %s>" % self.cls


class ResourceMeta(type):
    """Do some work for resources."""

    def __new__(mcs, name, bases, params):
        """Initialize class."""
        cls = super(ResourceMeta, mcs).__new__(mcs, name, bases, params)
        cls.methods = set([method.upper() for method in cls.methods])
        cls.meta = cls.OPTIONS_CLASS(cls)
        return cls


class Resource(View, metaclass=ResourceMeta):

    OPTIONS_CLASS = ResourceOptions

    # methods: Allowed methods
    methods = 'get',

    # Schema: Resource marshmallow schema
    Schema = None

    class Meta:
        """Tune the resource."""

        # name: Resource's name (if it is None, it will be calculated)
        name = None

        # per_page: Paginate results (set to None for disable pagination)
        per_page = 100

        # link_header: Add Link header with pagination
        page_link_header = False

        # url: URL for collection, if it is None it will be calculated
        # url_detail: URL for resource detail, if it is None it will be calculated
        url = url_detail = None

        # Resource filters
        filters = ()

        # Define allowed resource sorting params
        sorting = ()

        # Filters converter class
        filters_converter = Filters

        # Strict mode (only allowed query params) set to list of names for allowed query params
        strict = False

        # Swagger specs
        specs = None

        # marshmallow.Schema.Meta options
        # -------------------------------

        # Redefine Schema.Meta completely
        schema_meta = None

    def __init__(self, api=None, raw=False, **kwargs):
        """Initialize the resource."""
        self.api = api
        self.raw = raw
        self.auth = self.collection = None
        super(Resource, self).__init__(**kwargs)

    @classmethod
    def from_func(cls, func, methods=None, **params):

        if methods is None:
            methods = ['GET']

        def proxy(self, *args, **kwargs):
            return func(self, *args, **kwargs)

        for m in methods:
            params[m.lower()] = proxy
        params['methods'] = methods
        params['__doc__'] = func.__doc__
        return type(func.__name__, (cls,), params)

    def dispatch_request(self, *args, **kwargs):
        """Process current request."""
        if self.meta.strict and not (self.meta.strict >= set(request.args)):
            raise APIError('Invalid query params.')

        self.auth = self.authorize(*args, **kwargs)
        self.collection = self.get_many(*args, **kwargs)

        kwargs['resource'] = resource = self.get_one(*args, **kwargs)

        endpoint = kwargs.pop('endpoint', None)
        if endpoint and hasattr(self, endpoint):
            method = getattr(self, endpoint)
            logger.debug('Loaded endpoint: %s', endpoint)
            response = method(*args, **kwargs)
            return self.to_json_response(response)

        headers = {}

        if request.method == 'GET' and resource is None:

            # Filter resources
            self.collection = self.filter(self.collection, *args, **kwargs)

            # Sort resources
            if SORT_ARG in request.args:
                sorting = ((name.strip('-'), name.startswith('-'))
                           for name in request.args[SORT_ARG].split(','))
                sorting = (
                    (self.meta.sorting.get(n), d) for n, d in sorting if n in self.meta.sorting)
                self.collection = self.sort(self.collection, *sorting, **kwargs)

            # Paginate resources
            if self.meta.per_page:
                try:
                    per_page = int(request.args.get(PER_PAGE_ARG, self.meta.per_page))
                    if per_page:
                        page = int(request.args.get(PAGE_ARG, 0))
                        offset = page * per_page
                        self.collection, total = self.paginate(offset, per_page)
                        headers = make_pagination_headers(
                            per_page, page, total, self.meta.page_link_header)
                except ValueError:
                    raise APIError('Pagination params are invalid.')

        if logger.level <= logging.DEBUG:
            logger.debug('Collection: %r', self.collection)
            logger.debug('Params: %r', kwargs)

        try:
            method = getattr(self, request.method.lower())
        except AttributeError:
            return abort(405)

        response = method(*args, **kwargs)
        return self.to_json_response(response, headers=headers)

    def to_json_response(self, response, headers=None):
        """Serialize simple response to Flask response."""
        if self.raw or isinstance(response, Response):
            return response
        response = current_app.response_class(
            dumps(response, indent=2), mimetype='application/json')
        if headers:
            response.headers.extend(headers)
        return response

    def authorize(self, *args, **kwargs):
        """Default authorization method."""
        if self.api is not None:
            return self.api.authorize(self, *args, **kwargs)
        return current_user

    def get_many(self, *args, **kwargs):
        """Get collection."""
        return []

    def get_one(self, *args, **kwargs):
        """Load resource."""
        return kwargs.get(self.meta.name)

    def get_schema(self, resource=None, **kwargs):
        """Get schema."""
        return self.Schema and self.Schema()  # noqa

    def filter(self, collection, *args, **kwargs):
        """Filter collection."""
        return self.meta.filters.filter(collection, self, *args, **kwargs)

    def sort(self, collection, *sorting, **kwargs):
        """Sort collection."""
        logger.debug('Sort collection: %r', sorting)
        return collection

    def load(self, data, resource=None, **kwargs):
        """Load given data into schema."""
        schema = self.get_schema(resource=resource, **kwargs)
        resource, errors = schema.load(data, partial=resource is not None)
        if errors:
            raise APIError('Bad request', payload={'errors': errors})
        return resource

    def save(self, resource):
        """Create a resource."""
        return resource

    def to_simple(self, data, many=False, **kwargs):
        """Serialize response to simple object (list, dict)."""
        schema = self.get_schema(many=many, **kwargs)
        return schema.dump(data, many=many).data if schema else data

    def paginate(self, offset, limit):
        """Paginate results."""
        logger.debug('Paginate collection, offset: %d, limit: %d', offset, limit)
        return self.collection[offset: offset + limit], len(self.collection)

    def get(self, resource=None, **kwargs):
        logger.debug('Get resources (%r)', resource)
        if resource is not None and resource != '':
            return self.to_simple(resource, resource=resource, **kwargs)

        return self.to_simple(self.collection, many=True, **kwargs)

    def post(self, **kwargs):
        data = request.json or {}
        resource = self.load(data, **kwargs)
        resource = self.save(resource)
        logger.debug('Create a resource (%r)', kwargs)
        return self.to_simple(resource, **kwargs)

    def put(self, resource=None, **kwargs):
        """Update a resource."""
        logger.debug('Update a resource (%r)', resource)
        if resource is None:
            raise APIError('Resource not found', status_code=404)

        return self.post(resource=resource, **kwargs)

    patch = put

    def delete(self, resource=None, **kwargs):
        logger.debug('Delete a resource (%r)', resource)
        if resource is None:
            raise APIError('Resource not found', status_code=404)
        self.collection.remove(resource)

    @classmethod
    def update_specs(cls, specs):
        schema_name = cls.meta.name
        if cls.Schema:
            schema_name = cls.Schema.__name__.replace('Schema', '')
            if schema_name not in specs.components._schemas:
                specs.components.schema(schema_name, schema=cls.Schema)

        operations = yaml_utils.load_operations_from_docstring(cls.__doc__)

        openapi_url = convert_flask_url_to_openapi(cls.meta.url)
        path_parameters = cls.build_path_parameters(openapi_url)
        operations_spec = cls.update_operations_specs(
            operations, ('GET', 'POST'), detail=None if not cls.meta.url_detail else False, parameters=path_parameters)
        specs.path(openapi_url, operations=operations_spec)

        if cls.meta.url_detail:
            openapi_url = convert_flask_url_to_openapi(cls.meta.url_detail)
            path_parameters = cls.build_path_parameters(openapi_url)
            operations_spec = cls.update_operations_specs(
                operations, ('GET', 'PUT', 'PATCH', 'DELETE'), detail=True, parameters=path_parameters)
            specs.path(openapi_url, operations=operations_spec)

        for endpoint, (url_, name_, params_) in cls.meta.endpoints.values():
            openapi_url = convert_flask_url_to_openapi(f"{cls.meta.url.rstrip('/')}/{url_}")
            path_parameters = cls.build_path_parameters(openapi_url)
            operations_spec = cls.update_operations_specs(
                operations,
                params_.get('methods', ('GET',)),
                method=getattr(cls, name_, None),
                detail=None,
                parameters=path_parameters
            )
            specs.path(openapi_url, operations=operations_spec)

    @classmethod
    def update_operations_specs(cls, operations, methods, method=None, detail=None, **specs):
        operations = operations or {}
        result = {}
        for method_name in methods:
            if method is None and method_name not in cls.methods:
                continue

            method_name = method_name.lower()
            cls_method = method or getattr(cls, method_name, None)
            if not cls_method:
                continue

            defaults = dict(deepcopy(specs))
            defaults.setdefault('tags', [cls.meta.name])
            default_parameters = cls.build_query_parameters(method_name, detail)
            if default_parameters:
                defaults['parameters'] = (defaults.get('parameters') or []) + default_parameters

            docstring = clean_doc(cls_method.__doc__, cls.__doc__)
            if docstring:
                defaults.setdefault('summary', docstring.split('\n')[0])
                defaults.setdefault('description', docstring)

            defaults.setdefault('responses', {
                200: {
                    'description': 'OK',
                    'content': {'application/json': {}},
                    'headers': cls.build_response_headers(method_name, detail)
                }
            })
            if cls.Schema:
                schema_name = cls.Schema.__name__.replace('Schema', '')
                defaults['responses'][200]['content']['application/json']['schema'] \
                    = {'$ref': '#/components/schemas/%s' % schema_name}

            if method_name in ('put', 'patch', 'post'):
                defaults.setdefault('parameters', [])
                schema = {}
                if cls.Schema:
                    schema_name = cls.Schema.__name__.replace('Schema', '')
                    schema['$ref'] = '#/components/schemas/%s' % schema_name

                defaults['requestBody'] = {
                    'description': 'Request Body',
                    'required': True,
                    'content': {
                        'application/json': {
                            'schema':   schema
                        }
                    }
                }

            if method_name in operations:
                defaults.update(operations[method_name])

            docstring_yaml = yaml_utils.load_yaml_from_docstring(cls_method.__doc__)
            if docstring_yaml:
                defaults.update(docstring_yaml)

            result[method_name] = defaults
        return result

    @classmethod
    def build_path_parameters(cls, url: str):
        matches = re.findall(r'/{([^{/]+)}', url)
        descriptions = cls.describe_path_parameters()
        return [
            {
                'name': parameter,
                'in': 'path',
                'description': descriptions.get(parameter),
                'required': True,
                'schema': {'type': 'string'}
            }
            for parameter in matches
        ]

    @classmethod
    def build_response_headers(cls, method: str, detail: Optional[bool]) -> dict:
        if method == 'get' and detail is False:
            return {
                'X-Total-Count': {
                    'description': 'Total number of results',
                    'schema': {'type': 'integer'}
                },
                'X-Limit': {
                    'description': 'Number results per page. Requests that return multiple items ' +
                                'will be paginated to 100 items by default. ' + 
                                'You can specify further pages with the `?page` parameter. ' + 
                                'For some resources, you can also set a custom page ' + 
                                'size up to 1000 with the `?per_page` parameter.',
                    'schema': {'type': 'integer'}
                },
                'X-Page-Last': {
                    'description': 'Number of last page',
                    'schema': {'type': 'integer'}
                },
                'X-Page': {
                    'description': 'Current Page Number (starts from 0)',
                    'schema': {'type': 'integer'}
                }
            }
        return {}

    @classmethod
    def build_query_parameters(cls, method: str, detail: Optional[bool]) -> list:
        if method == 'get' and detail is False:
            return [
                {
                    'name': PAGE_ARG,
                    'in': 'query',
                    'description': 'Page number of dataset',
                    'required': False,
                    'schema': {'type': 'integer', 'minimum': 0}
                },
                {
                    'name': PER_PAGE_ARG,
                    'in': 'query',
                    'description': 'Custom page size of dataset',
                    'required': False,
                    'schema': {'type': 'integer', 'default': 100}
                },
                {
                    'name': SORT_ARG,
                    'in': 'query',
                    'description': 'String with a comma-separated list of parameters by which to sort the dataset. ' + 
                                   'To specify a descending order, add a "-" character before the parameter. ' +
                                   'For example, `?sort=-created_at,id`',
                    'required': False,
                    'schema': {'type': 'string', 'example': ''},
                }
            ]

        return []
        
    @classmethod
    def describe_path_parameters(cls):
        descriptions = {}
        if cls.meta.url_detail:
            descriptions[cls.meta.name] = 'Resource Identifier'
        return descriptions


def make_pagination_headers(limit, curpage, total, link_header=True):
    """Return Link Hypermedia Header."""
    lastpage = int(math.ceil(1.0 * total / limit) - 1)
    headers = {'X-Total-Count': str(total), 'X-Limit': str(limit),
               'X-Page-Last': str(lastpage), 'X-Page': str(curpage)}

    if not link_header:
        return headers

    base = "{}?%s".format(request.path)
    links = {}
    links['first'] = base % urlencode(dict(request.args, **{PAGE_ARG: 0}))
    links['last'] = base % urlencode(dict(request.args, **{PAGE_ARG: lastpage}))
    if curpage:
        links['prev'] = base % urlencode(dict(request.args, **{PAGE_ARG: curpage - 1}))
    if curpage < lastpage:
        links['next'] = base % urlencode(dict(request.args, **{PAGE_ARG: curpage + 1}))

    headers['Link'] = ",".join(['<%s>; rel="%s"' % (v, n) for n, v in links.items()])
    return headers


def clean_doc(*values):
    """Clean doc string."""
    for v in values:
        v = v and v.split('---')[0].strip()
        if v:
            return v

    return None


def convert_flask_url_to_openapi(flask_url: str):
    return RE_URL.sub(r'{\1}', flask_url)

# pylama:ignore=R0201
