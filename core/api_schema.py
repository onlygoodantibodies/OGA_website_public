"""The OpenAPI 3.1 description of this API, served at ``/api/v1/openapi.json``.

Why this is Python and not a checked-in ``openapi.yaml``
-------------------------------------------------------
A specification is a promise about what the code does, and a hand-maintained one
becomes a lie at the first commit nobody remembers to mirror into it. Everything
here that is also a fact in the code is **read from the code**: the rate limits
from ``api_throttle``, the recommendation values and their meanings from
``recommendations``, the CSV column order from ``api_manifest``, the four
applications from ``recommendations.APPLICATIONS``. Change one of those and the published contract moves
with it, in the same commit, without anybody remembering anything.

What is left to drift is prose and response shapes, and
``core/tests_api_schema.py`` closes most of that: every path in the document
must resolve to a real Django view, every operation must name a security scheme
that exists, and the enums must equal the constants they came from.

Why JSON and not YAML
---------------------
YAML would need PyYAML, which this project does not depend on, to serve a format
that every OpenAPI tool already accepts as JSON. Swagger UI, Redoc, Postman,
Insomnia and ``openapi-generator`` all read this document as it stands.

Version
-------
``info.version`` is the API's, not the site's. Bump it when the contract
changes; the manifest carries its own ``manifest_version`` for the body shape.
"""
from __future__ import annotations

from . import api_throttle, recommendations as R
from .api_manifest import CSV_COLUMNS, MANIFEST_VERSION
from .api_views import BASE_URL

# 2.0.0 because a field was **removed**: `verdicts`, the deprecated alias of
# `oga_recommendations`, went on 7 Aug 2026. Nothing outside OGA held a key, so
# nobody broke — but the number describes the artefact, not the audience, and a
# removal signalled as a minor bump is the same class of error as a document
# describing a field that is not there. Not to be confused with the `/v1/` in
# the URL: that is the path, and it has not moved.
API_VERSION = '2.0.0'

_SERVER = f'{BASE_URL}/api/v1'


def _recommendation_schema():
    """The three-valued recommendation, meanings taken from the one reader."""
    return {
        'type': 'string',
        'enum': [R.RECOMMENDED, R.NOT_RECOMMENDED, R.NOT_TESTED],
        'description': (
            "OGA's recommendation for one application.\n\n"
            + '\n'.join(f'- `{value}` - {R.MEANINGS[value]}'
                        for value in (R.RECOMMENDED, R.NOT_RECOMMENDED,
                                      R.NOT_TESTED))
            + '\n\n' + R.SCOPE_NOTE),
        'example': R.RECOMMENDED,
    }


def _rate_limit_headers():
    return {
        'X-RateLimit-Limit-Burst': {
            'schema': {'type': 'integer', 'example': api_throttle.BURST_LIMIT},
            'description': 'Requests allowed per minute.'},
        'X-RateLimit-Remaining-Burst': {
            'schema': {'type': 'integer'},
            'description': 'Requests left in the current minute.'},
        'X-RateLimit-Limit-Sustained': {
            'schema': {'type': 'integer',
                       'example': api_throttle.SUSTAINED_LIMIT},
            'description': 'Requests allowed per hour.'},
        'X-RateLimit-Remaining-Sustained': {
            'schema': {'type': 'integer'},
            'description': 'Requests left in the current hour.'},
    }


def _recommendation_scope():
    """The envelope field saying what the recommendations beside it cover.

    On the three responses that carry recommendations, and on the **envelope**
    rather than inside each row — a caveat on every row is a caveat nobody
    reads, which is ``recommendations.SCOPE_NOTE``'s own reasoning. It is also
    the half that made this safe to add to a live API: the per-antibody and
    per-gene objects are untouched, so anything iterating ``antibodies`` sees
    no change at all.

    Named ``recommendation_scope`` and **not** ``scope``, because the manifest
    already uses ``scope`` for which *part of the dataset* a key covers
    (``api_manifest.py``). One word with two meanings on one API is the drift
    this file exists to prevent, and ``api_manifest`` had already chosen this
    name for this string.
    """
    return {
        'type': 'string',
        'example': R.SCOPE_NOTE,
        'description': (
            'What an OGA recommendation does and does not cover. The same '
            'sentence on every response, and the same one the public pages '
            'carry.'),
    }


def _schemas():
    applications = list(R.APPLICATIONS)
    return {
        'Recommendation': _recommendation_schema(),

        'OgaRecommendations': {
            'type': 'object',
            'description': 'One recommendation per application.',
            'properties': {app: {'$ref': '#/components/schemas/Recommendation'}
                           for app in applications},
        },

        'Recommendations': {
            'type': 'object',
            'deprecated': True,
            'description': (
                'The raw curated booleans, kept for existing integrations. '
                '`false` cannot distinguish "assessed and not recommended" from '
                '"never assessed" - use `oga_recommendations` instead.'),
            'properties': {app: {'type': 'boolean'} for app in applications},
        },

        'AntibodyMetadata': {
            'type': 'object',
            'properties': {
                'rrid': {'type': ['string', 'null'], 'example': 'AB_2687467'},
                'supplier': {'type': ['string', 'null'], 'example': 'Abcam'},
                'host': {'type': ['string', 'null'], 'example': 'Rabbit'},
                'clonality': {'type': ['string', 'null'],
                              'example': 'Recombinant monoclonal'},
                'clone_id': {'type': ['string', 'null']},
                'recombinant': {'type': ['string', 'null'],
                                'description': '"Yes" or null, for backward '
                                               'compatibility.'},
                'product_link': {'type': ['string', 'null'], 'format': 'uri'},
                'discontinued': {'type': 'boolean'},
            },
        },

        'Experiment': {
            'type': 'object',
            'description': 'One published figure.',
            'properties': {
                'experiment_type': {'type': 'string', 'enum': applications},
                'experiment_type_display': {'type': 'string',
                                            'example': 'Western Blot'},
                'image_url': {
                    'type': ['string', 'null'], 'format': 'uri',
                    'description': 'Public, permanent, no key required. Send a '
                                   'User-Agent: bot protection in front of the '
                                   'CDN can refuse a default library one and '
                                   'the 403 looks like a permissions problem.'},
            },
        },

        'Antibody': {
            'type': 'object',
            'properties': {
                'antibody_name': {'type': 'string', 'example': 'ab138501',
                                  'description': 'The catalogue number.'},
                'gene': {'type': 'string', 'example': 'ACE'},
                'gene_has_recommendations': {'type': ['boolean', 'null']},
                'gene_page_url': {'type': 'string', 'format': 'uri'},
                'created_at': {'type': ['string', 'null'],
                               'format': 'date-time'},
                'metadata': {'$ref': '#/components/schemas/AntibodyMetadata'},
                'recommendations': {
                    '$ref': '#/components/schemas/Recommendations'},
                'oga_recommendations': {
                    '$ref': '#/components/schemas/OgaRecommendations'},
                'experiments': {
                    'type': 'array',
                    'items': {'$ref': '#/components/schemas/Experiment'}},
                'embed_urls': {'type': ['object', 'null'],
                               'additionalProperties': {'type': 'string'}},
            },
        },

        'ManifestFile': {
            'type': 'object',
            'description': 'One downloadable figure. The CSV format carries '
                           'these same fields in this order: '
                           + ', '.join(CSV_COLUMNS) + '.',
            'properties': {
                'image_id': {'type': 'integer'},
                'url': {'type': 'string', 'format': 'uri',
                        'description': 'Public and permanent. Fetch it '
                                       'directly; no key is needed.'},
                'filename': {'type': 'string',
                             'description': 'Your filename pattern applied, '
                                            'de-duplicated if it collides.'},
                'application': {'type': 'string', 'enum': applications},
                'application_display': {'type': 'string'},
                'oga_recommendation': {
                    '$ref': '#/components/schemas/Recommendation'},
                'gene': {'type': 'string'},
                'catalogue_number': {'type': 'string'},
                'rrid': {'type': ['string', 'null']},
                'supplier': {'type': ['string', 'null']},
                'product_link': {'type': ['string', 'null'], 'format': 'uri'},
                'discontinued': {'type': 'boolean'},
                'gene_page_url': {'type': 'string', 'format': 'uri'},
                'added_at': {'type': ['string', 'null'], 'format': 'date-time'},
            },
        },

        'Manifest': {
            'type': 'object',
            'required': ['manifest_version', 'dataset_version', 'complete',
                         'files', 'sync'],
            'properties': {
                'manifest_version': {'type': 'integer',
                                     'example': MANIFEST_VERSION},
                'generated_at': {'type': 'string', 'format': 'date-time'},
                'dataset_version': {
                    'type': 'string',
                    'description': 'Identifies the data. Two clients holding '
                                   'the same value hold the same figures. NOT '
                                   'the ETag - that also identifies the '
                                   'format.'},
                'consumer': {'type': 'string'},
                'consumer_type': {'type': 'string',
                                  'enum': ['manufacturer', 'rrid']},
                'scope': {
                    'type': 'object',
                    'properties': {
                        'genes': {'type': ['array', 'null'],
                                  'items': {'type': 'string'}},
                        'supplier_filter': {'type': ['string', 'null']},
                        'includes_recommendations': {'type': 'boolean'},
                        'recommendation_scope': {'type': 'string'},
                        'requested_genes': {
                            'type': ['array', 'null'],
                            'items': {'type': 'string'},
                            'description': 'What `?gene=` asked for, echoed. '
                                           'Null when the request was not '
                                           'narrowed.'},
                    }},
                'counts': {
                    'type': 'object',
                    'properties': {
                        'files_in_scope': {'type': 'integer'},
                        'files_matched': {'type': 'integer'},
                        'files_returned': {'type': 'integer'},
                    }},
                'complete': {
                    'type': 'boolean',
                    'description': (
                        '**Read this before treating a missing URL as a '
                        'deletion.** True means the response holds every file '
                        'in your scope. False means it was capped or filtered, '
                        'and absence means nothing.')},
                'filename_collisions': {'type': 'integer'},
                'files': {'type': 'array',
                          'items': {'$ref': '#/components/schemas/ManifestFile'}},
                'sync': {
                    'type': 'object',
                    'properties': {
                        'mode': {'type': 'string',
                                 'enum': ['full', 'incremental']},
                        'etag': {'type': 'string',
                                 'description': 'Send this back verbatim as '
                                                'If-None-Match.'},
                        'reconnect_with': {'type': 'string'},
                        'deletions_tracked': {'type': 'boolean'},
                        'image_format': {'type': 'string',
                                         'enum': ['original']},
                    }},
            },
        },

        'Error': {
            'type': 'object',
            'required': ['error'],
            'properties': {
                'error': {'type': 'string'},
                'retry_after_seconds': {'type': 'integer'},
                'your_consumer_type': {'type': 'string'},
            },
        },
    }


def _paths():
    ok = 'Success.'
    tag_data, tag_meta, tag_account = ['Data'], ['Discovery'], ['Account']

    def json_response(schema_ref, description=ok):
        return {'description': description,
                'content': {'application/json': {
                    'schema': {'$ref': schema_ref}}}}

    common_errors = {
        '401': {'$ref': '#/components/responses/Unauthorized'},
        '403': {'$ref': '#/components/responses/Forbidden'},
        '429': {'$ref': '#/components/responses/RateLimited'},
    }

    return {
        '/': {
            'get': {
                'tags': tag_meta,
                'summary': 'Endpoint catalogue',
                'description': 'What this API offers. Needs no key.',
                'security': [],
                'operationId': 'getCatalogue',
                'responses': {'200': {'description': ok}},
            }},

        '/openapi.json': {
            'get': {
                'tags': tag_meta,
                'summary': 'This document',
                'security': [],
                'operationId': 'getOpenApiDocument',
                'responses': {'200': {'description': ok}},
            }},

        '/manifest/': {
            'get': {
                'tags': tag_data,
                'summary': 'Download manifest',
                'operationId': 'getManifest',
                'description': (
                    'Every published figure you may download, as URLs with '
                    'their metadata. **Contains no image bytes** - the URLs are '
                    'public and permanent, so fetch them directly from the CDN.'
                    '\n\nComplete by default and carries a strong `ETag`. Send '
                    'it back as `If-None-Match` on reconnect and an unchanged '
                    'dataset answers `304` with no body. The ETag covers image '
                    'identity, stored filename and every recommendation in '
                    'scope, so a replaced figure and a changed recommendation '
                    'both break it - which a timestamp cursor cannot.\n\n'
                    'No side effects: it '
                    'never advances your review cursor.'),
                'parameters': [
                    {'name': 'format', 'in': 'query',
                     'schema': {'type': 'string',
                                'enum': ['json', 'csv', 'urls'],
                                'default': 'json'},
                     'description': '`urls` is one bare URL per line, for '
                                    '`wget -i`.'},
                    {'name': 'since', 'in': 'query',
                     'schema': {'type': 'string'},
                     'description': 'ISO 8601 or YYYY-MM-DD. Additions only. '
                                    '**Cannot report removals or replacements**, '
                                    'and sets `complete: false`.'},
                    {'name': 'gene', 'in': 'query',
                     'schema': {'type': 'string', 'example': 'ACE,ANXA11'},
                     'description': 'Comma-separated gene symbols. Keeps '
                                    '`complete: true` — the reply is every '
                                    'file for those genes — and sets '
                                    '`sync.narrowed_by_request`. Diff it '
                                    'against a mirror of those genes only: '
                                    'against a wider mirror, every other '
                                    'gene reads as withdrawn.'},
                    {'name': 'limit', 'in': 'query',
                     'schema': {'type': 'integer', 'minimum': 1},
                     'description': 'Caps the reply and sets `complete: false`.'},
                    {'name': 'offset', 'in': 'query',
                     'schema': {'type': 'integer', 'minimum': 0}},
                    {'name': 'If-None-Match', 'in': 'header',
                     'schema': {'type': 'string'},
                     'description': 'The `etag` from a previous reply, verbatim.'},
                ],
                'responses': {
                    '200': dict(json_response('#/components/schemas/Manifest'),
                                headers={'ETag': {'schema': {'type': 'string'}},
                                         **_rate_limit_headers()}),
                    '304': {'description': 'Nothing has changed since the ETag '
                                           'you sent. Empty body.'},
                    '400': {'$ref': '#/components/responses/BadRequest'},
                    **common_errors,
                },
            }},

        '/download/': {
            'get': {
                'tags': tag_data,
                'summary': 'Download everything as one file',
                'operationId': 'downloadArchive',
                'description': (
                    'The whole public dataset as a single zip: every published '
                    'figure, plus `manifest.csv` naming each one.\n\n**This '
                    'endpoint answers `302`.** The archive lives in object '
                    'storage, so the redirect points at a CDN URL that supports '
                    'range requests and resuming - no bytes pass through the '
                    'application. Any ordinary HTTP client follows it; `curl` '
                    'needs `-L`.\n\nThe archive is rebuilt when the dataset '
                    'changes, and its URL carries the dataset version, so a URL '
                    'you have already fetched is safe to cache indefinitely. '
                    'Ask this endpoint again to learn the current one.\n\n`503` '
                    'means a figure has been published since the last build and '
                    'the archive for this version does not exist yet. Nothing '
                    'is missing - the manifest is complete throughout, and the '
                    'reply names it.\n\nAdd `?gene=` to get a zip of named '
                    'genes instead, built on request. A key scoped to part of '
                    'the dataset always gets a zip built this way rather than a '
                    'redirect, because the shared archive holds every '
                    'supplier.'),
                'parameters': [
                    {'name': 'gene', 'in': 'query',
                     'schema': {'type': 'string'},
                     'description': 'Comma-separated gene symbols. Returns a zip '
                                    'directly (200) rather than redirecting.'},
                ],
                'responses': {
                    '302': {'description': 'Follow the `Location` header to the '
                                           'prepared archive.',
                            'headers': {'Location': {'schema': {'type': 'string',
                                                                'format': 'uri'}}}},
                    '200': {'description': 'A zip of the requested figures.',
                            'content': {'application/zip': {
                                'schema': {'type': 'string', 'format': 'binary'}}}},
                    '404': {'description': 'Nothing in scope matches.'},
                    '413': {'description': 'Too many figures to build on '
                                           'request. Use the manifest, or the '
                                           'prepared archive.'},
                    '503': {'description': 'The archive for this dataset version '
                                           'is not built yet. Use the manifest '
                                           'in the meantime.'},
                    **common_errors,
                },
            }},

        '/antibodies/': {
            'get': {
                'tags': tag_data,
                'summary': 'Antibody records',
                # Needs no key (owner, 12 Aug 2026). A key is still read when
                # sent — it names the caller, applies a supplier filter and
                # moves a cursor — so this says "optional", which is what an
                # empty `security` beside a global scheme means.
                'security': [],
                'operationId': 'listAntibodies',
                'description': (
                    'Antibody metadata with per-application recommendations and '
                    'figure URLs.\n\n**This endpoint has a side effect.** Called '
                    'without `preview=true` it returns only what is new since '
                    'your last call and *advances your review cursor*, and is '
                    'limited to one such call an hour - so a client that fails '
                    'while parsing loses that delta. Use `preview=true`, or '
                    '`advance_cursor=false`, or prefer `/manifest/` with an '
                    'ETag, which is idempotent.'),
                'parameters': [
                    {'name': 'preview', 'in': 'query',
                     'schema': {'type': 'boolean'},
                     'description': 'Full set, no hourly gate, cursor untouched.'},
                    {'name': 'advance_cursor', 'in': 'query',
                     'schema': {'type': 'boolean', 'default': True},
                     'description': 'false reads the delta without consuming it.'},
                    {'name': 'since', 'in': 'query',
                     'schema': {'type': 'string'}},
                    {'name': 'gene', 'in': 'query',
                     'schema': {'type': 'string'}, 'example': 'ACE'},
                    {'name': 'application', 'in': 'query',
                     'schema': {'type': 'string', 'enum': list(R.APPLICATIONS)},
                     'description': 'Recommended for that application.'},
                    {'name': 'recommended_only', 'in': 'query',
                     'schema': {'type': 'boolean'}},
                    {'name': 'limit', 'in': 'query',
                     'schema': {'type': 'integer', 'minimum': 1}},
                    {'name': 'offset', 'in': 'query',
                     'schema': {'type': 'integer', 'minimum': 0}},
                ],
                'responses': {
                    '200': {
                        'description': ok,
                        'headers': _rate_limit_headers(),
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'consumer': {'type': 'string'},
                                'count': {'type': 'integer'},
                                'matched': {'type': 'integer'},
                                'complete': {'type': 'boolean'},
                                'cursor_advanced': {'type': 'boolean'},
                                'preview': {'type': 'boolean'},
                                'since': {'type': ['string', 'null']},
                                'recommendation_scope': _recommendation_scope(),
                                'antibodies': {
                                    'type': 'array',
                                    'items': {'$ref':
                                              '#/components/schemas/Antibody'}},
                            }}}}},
                    '400': {'$ref': '#/components/responses/BadRequest'},
                    **common_errors,
                },
            }},

        '/genes/': {
            'get': {
                'tags': tag_data,
                'summary': 'Gene catalogue',
                'security': [],
                'operationId': 'listGenes',
                'description': (
                    'Genes with knockout-controlled data - a named gene with at '
                    'least one antibody carrying a published figure. Genes the '
                    'lab merely intends to work on are not included.'),
                'parameters': [
                    {'name': 'since', 'in': 'query',
                     'schema': {'type': 'string'},
                     'description': 'Switches the reply to `mode: incremental`.'},
                    {'name': 'has_recommendations', 'in': 'query',
                     'schema': {'type': 'boolean'}},
                ],
                'responses': {
                    '200': {
                        'description': ok,
                        'headers': _rate_limit_headers(),
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'count': {'type': 'integer'},
                                'mode': {'type': 'string',
                                         'enum': ['full', 'incremental']},
                                'since': {'type': ['string', 'null']},
                                'recommendation_scope': _recommendation_scope(),
                                'genes': {'type': 'array', 'items': {
                                    'type': 'object',
                                    'properties': {
                                        'gene': {'type': 'string'},
                                        'gene_page_url': {'type': 'string'},
                                        'f1000_report': {
                                            'type': ['string', 'null']},
                                        'has_recommendations': {
                                            'type': 'boolean'},
                                        'antibody_count': {'type': 'integer'},
                                        'experiment_count': {'type': 'integer'},
                                        'recommendations_by_application': {
                                            'type': 'object',
                                            'additionalProperties': {
                                                'type': 'integer'}},
                                    }}},
                            }}}}},
                    **common_errors,
                },
            }},

        '/gene-detail/': {
            'get': {
                'tags': tag_data,
                'summary': 'Competitor view (manufacturers only)',
                'operationId': 'getGeneDetail',
                'description': (
                    'Every antibody against one gene, grouped by supplier, with '
                    'a recommended-versus-total summary per vendor.\n\n'
                    '**Restricted by who is asking, not by what they pay.** '
                    'Only `consumer_type: manufacturer` may call it; anyone '
                    'else gets 403 with `your_consumer_type` naming theirs.'),
                'parameters': [
                    {'name': 'gene', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'}, 'example': 'ACE'},
                ],
                'responses': {
                    '200': {
                        'description': ok,
                        'headers': _rate_limit_headers(),
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'gene': {'type': 'string'},
                                'total_antibodies': {'type': 'integer'},
                                'consumer_suppliers': {
                                    'type': 'array',
                                    'items': {'type': 'string'}},
                                'supplier_summary': {
                                    'type': 'object',
                                    'additionalProperties': {
                                        'type': 'object',
                                        'properties': {
                                            'count': {'type': 'integer'},
                                            'recommended': {'type': 'integer'},
                                        }}},
                                'recommendation_scope': _recommendation_scope(),
                                'antibodies': {
                                    'type': 'array',
                                    'items': {'$ref':
                                              '#/components/schemas/Antibody'}},
                            }}}}},
                    '400': {'$ref': '#/components/responses/BadRequest'},
                    '404': {'description': 'No such gene.'},
                    **common_errors,
                },
            }},

        '/status/': {
            'get': {
                'tags': tag_account,
                'summary': 'Your account and the dataset totals',
                'operationId': 'getStatus',
                'responses': {
                    '200': {
                        'description': ok,
                        'headers': _rate_limit_headers(),
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'consumer': {'type': 'string'},
                                'consumer_type': {'type': 'string'},
                                'supplier_filter': {'type': ['string', 'null']},
                                'gene_filter': {'type': ['string', 'null']},
                                'last_queried_at': {
                                    'type': ['string', 'null'],
                                    'format': 'date-time'},
                                'pending_antibodies': {'type': 'integer'},
                                'total_genes': {'type': 'integer'},
                                'total_antibodies': {'type': 'integer'},
                                'total_experiments': {'type': 'integer'},
                            }}}}},
                    **common_errors,
                },
            }},

        '/mark-reviewed/': {
            'post': {
                'tags': tag_account,
                'summary': 'Move your review cursor to now',
                'operationId': 'markReviewed',
                'responses': {'200': {'description': ok}, **common_errors},
            }},

        '/reviewed/': {
            'get': {
                'tags': tag_account,
                'summary': 'Catalogue numbers you have marked reviewed',
                'operationId': 'listReviewed',
                'responses': {'200': {'description': ok}, **common_errors},
            },
            'post': {
                'tags': tag_account,
                'summary': 'Mark antibodies reviewed',
                'operationId': 'addReviewed',
                'requestBody': {'required': True, 'content': {
                    'application/json': {'schema': {
                        'type': 'object',
                        'required': ['catalogues'],
                        'properties': {'catalogues': {
                            'type': 'array', 'items': {'type': 'string'}}}}}}},
                'responses': {'200': {'description': ok},
                              '400': {'$ref': '#/components/responses/BadRequest'},
                              **common_errors},
            }},

        '/reviewed/clear/': {
            'delete': {
                'tags': tag_account,
                'summary': 'Clear every per-antibody review',
                'operationId': 'clearReviewed',
                'responses': {'200': {'description': ok}, **common_errors},
            }},

        '/portal-config/': {
            'get': {
                'tags': tag_account,
                'summary': 'Your saved preferences',
                'operationId': 'getPortalConfig',
                'responses': {'200': {'description': ok}, **common_errors},
            },
            'put': {
                'tags': tag_account,
                'summary': 'Save preferences',
                'operationId': 'setPortalConfig',
                'description': '`filename_pattern` decides the `filename` field '
                               'in the manifest. Placeholders: `{gene}`, '
                               '`{catalogue}`, `{rrid}`, `{application}`.',
                'requestBody': {'required': True, 'content': {
                    'application/json': {'schema': {
                        'type': 'object',
                        'properties': {
                            'filename_pattern': {'type': 'string'},
                            'url_pattern': {'type': 'string'},
                            'image_format': {'type': 'string'},
                            'contact_email': {'type': 'string',
                                              'format': 'email'},
                        }}}}},
                'responses': {'200': {'description': ok},
                              '400': {'$ref': '#/components/responses/BadRequest'},
                              **common_errors},
            }},

        '/report-issue/': {
            'post': {
                'tags': tag_account,
                'summary': 'Flag a problem with a record',
                'operationId': 'reportIssue',
                'description': 'Open to every account. Sends an email to the '
                               'OGA team.',
                'requestBody': {'required': True, 'content': {
                    'application/json': {'schema': {
                        'type': 'object',
                        'required': ['catalogue', 'gene', 'issue_type'],
                        'properties': {
                            'catalogue': {'type': 'string'},
                            'gene': {'type': 'string'},
                            'rrid': {'type': 'string'},
                            'issue_type': {
                                'type': 'string',
                                'enum': ['recommendation', 'discontinued',
                                         'technical']},
                            'details': {'type': 'string'},
                        }}}}},
                'responses': {'200': {'description': 'Sent.'},
                              '400': {'$ref': '#/components/responses/BadRequest'},
                              **common_errors},
            }},

        # ── Pre-release: your own reagents, before publication ──────────────
        #
        # Documented in the same breath as the published feeds and marked as
        # unpublished in every summary, because the one way this goes wrong is
        # a partner building against it without noticing which half of the API
        # they are in.
        '/pipeline-data/': {
            'get': {
                'tags': tag_data,
                'summary': 'Your reagents\' figures, before they are published',
                'operationId': 'getPipelineData',
                'description': (
                    '**Unpublished data.** Figures of your own reagents that '
                    'have been cropped from a validation experiment and are '
                    'waiting for OGA\'s review meeting. They may change or be '
                    'withdrawn before release.\n\nThe window exists so that a '
                    'wrong catalogue number, a withdrawn lot or a mistaken RRID '
                    'can be caught before the public page goes live. Do not '
                    'quote, publish or link these, and do not read '
                    '`provisional_recommendation` as an OGA recommendation — '
                    'that verdict is not applied to the antibody until release.'
                    '\n\nRequires a key with a **supplier scope**. A key with no '
                    'supplier scope is refused with `403`: unscoped is not a '
                    'narrow scope, and this endpoint must never show one '
                    'manufacturer another\'s unpublished figures.'),
                'parameters': [{
                    'name': 'gene', 'in': 'query', 'required': False,
                    'schema': {'type': 'string'},
                    'description': 'Exact gene symbol.'}],
                'responses': {'200': {'description': ok}, **common_errors},
            }},

        '/pipeline-image/': {
            'get': {
                'tags': tag_data,
                'summary': 'The bytes of one pre-release figure',
                'operationId': 'getPipelineImage',
                'description': (
                    'Served by the application rather than from object '
                    'storage, and re-checked against your supplier scope on '
                    'every request — a URL is not a permission. Answers `404` '
                    'both for a figure that does not exist and for one that is '
                    'not yours, deliberately: telling you a figure exists but '
                    'belongs to somebody else is itself a fact about another '
                    'manufacturer\'s unpublished work.'),
                'parameters': [{
                    'name': 'id', 'in': 'query', 'required': True,
                    'schema': {'type': 'integer'},
                    'description': 'From `figures[].image_url` in /pipeline-data/.'}],
                'responses': {'200': {'description': 'The image.'},
                              '404': {'description': 'No pre-release figure of '
                                                     'yours with that id.'},
                              **common_errors},
            }},

        '/gene-progress/': {
            'get': {
                'tags': tag_data,
                'summary': 'Where the genes you have reagents in have got to',
                'operationId': 'getGeneProgress',
                'description': (
                    'One row per gene you have an antibody for — including '
                    'genes still being worked on, which is the case this '
                    'answers. Per gene: whether it has a public page, which of '
                    'the four applications have published figures, which have '
                    'figures of **yours** awaiting release, which sites have run '
                    'each procedure, and whether the report is published.\n\n'
                    'Every field is derived from records that exist rather than '
                    'from a status somebody typed, which is why it moves without '
                    'anybody updating anything.\n\nRequires a key with a '
                    'supplier scope.'),
                'parameters': [{
                    'name': 'gene', 'in': 'query', 'required': False,
                    'schema': {'type': 'string'},
                    'description': 'Exact gene symbol.'}],
                'responses': {'200': {'description': ok}, **common_errors},
            }},
    }


def document():
    """The OpenAPI 3.1 document, as a plain dict ready to serialise."""
    return {
        'openapi': '3.1.0',
        'info': {
            'title': 'OGA Data API',
            'version': API_VERSION,
            'summary': 'Knockout-controlled antibody characterisation data.',
            'description': (
                'Independently generated antibody characterisation from '
                'YCharOS, to community consensus protocols.\n\n'
                '### Before you display anything\n\n'
                'Recommendations are **per application** - a pass in western '
                'blot says nothing about immunofluorescence. ' + R.SCOPE_NOTE
                + '\n\n'
                '### Bulk download\n\n'
                'Use `/manifest/`. It lists every figure as a public, permanent '
                'URL with its metadata and no image bytes, so you fetch from a '
                'CDN rather than from us. Send its `ETag` back as '
                '`If-None-Match` and an unchanged dataset costs one `304`.'),
            'contact': {'name': 'Only Good Antibodies',
                        'email': 'onlygoodantibodies@gmail.com',
                        'url': BASE_URL},
        },
        'servers': [{'url': _SERVER, 'description': 'Production'}],
        'tags': [
            {'name': 'Discovery', 'description': 'No key required.'},
            {'name': 'Data', 'description': 'The dataset.'},
            {'name': 'Account',
             'description': 'Your scope, preferences and review state.'},
        ],
        'security': [{'ApiKeyAuth': []}],
        'paths': _paths(),
        'components': {
            'securitySchemes': {
                'ApiKeyAuth': {
                    'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key',
                    'description': (
                        'The key you were issued, as a header. The portal also '
                        'accepts `?key=` for bookmarked links; do not use that '
                        'from a program - query strings reach server logs, '
                        'browser history and the Referer of outbound clicks.'),
                }},
            'schemas': _schemas(),
            'responses': {
                'BadRequest': {
                    'description': 'A parameter could not be read.',
                    'content': {'application/json': {
                        'schema': {'$ref': '#/components/schemas/Error'}}}},
                'Unauthorized': {
                    'description': 'No X-API-Key header.',
                    'content': {'application/json': {
                        'schema': {'$ref': '#/components/schemas/Error'}}}},
                'Forbidden': {
                    'description': 'Key unknown or inactive, or this endpoint '
                                   'is not available to your account type.',
                    'content': {'application/json': {
                        'schema': {'$ref': '#/components/schemas/Error'}}}},
                'RateLimited': {
                    'description': (
                        f'Over {api_throttle.BURST_LIMIT}/minute or '
                        f'{api_throttle.SUSTAINED_LIMIT}/hour. Carries '
                        '`Retry-After` in seconds.'),
                    'headers': {'Retry-After': {'schema': {'type': 'integer'}},
                                **_rate_limit_headers()},
                    'content': {'application/json': {
                        'schema': {'$ref': '#/components/schemas/Error'}}}},
            },
        },
    }
