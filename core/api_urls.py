from django.urls import path
from . import api_manifest, api_not_supportive, api_pipeline, api_views

app_name = 'api'

urlpatterns = [
    # The catalogue and the download manifest — the machine-facing pair.
    path('v1/', api_manifest.api_index, name='api_index'),
    path('v1/manifest/', api_manifest.manifest, name='manifest'),
    path('v1/download/', api_manifest.download, name='download'),
    path('v1/openapi.json', api_manifest.openapi_document, name='openapi'),

    path('v1/antibodies/', api_views.antibodies_feed, name='antibodies_feed'),
    path('v1/genes/', api_views.genes_feed, name='genes_feed'),
    path('v1/gene-detail/', api_views.gene_detail, name='gene_detail'),
    path('v1/status/', api_views.api_status, name='api_status'),
    path('v1/portal-config/', api_views.portal_config_update, name='portal_config_update'),
    path('v1/mark-reviewed/', api_views.mark_reviewed, name='mark_reviewed'),
    path('v1/report-issue/', api_views.report_issue, name='report_issue'),
    path('v1/reviewed/', api_views.reviewed_antibodies, name='reviewed_antibodies'),
    path('v1/reviewed/clear/', api_views.clear_reviewed, name='clear_reviewed'),

    # Pre-release: figures of the caller's OWN reagents, before publication, and
    # where their genes have got to. Scoped keys only — see core/api_pipeline.py.
    path('v1/pipeline-data/', api_pipeline.pipeline_data, name='pipeline_data'),
    # ``?id=`` rather than a path segment: every other endpoint here takes its
    # arguments in the query string, and the OpenAPI document is checked by
    # resolving each documented path literally.
    path('v1/pipeline-image/', api_pipeline.pipeline_image, name='pipeline_image'),
    path('v1/gene-progress/', api_pipeline.gene_progress, name='gene_progress'),

    # The review list: every antibody in scope whose every tested application
    # came back without support, as rows and as a spreadsheet. Published data,
    # assembled — see
    # core/api_not_supportive.py for why a key with no supplier scope is
    # answered here and refused by /v1/pipeline-data/ next door.
    path('v1/not-supportive/', api_not_supportive.not_supportive,
         name='not_supportive'),
    path('v1/not-supportive/csv/', api_not_supportive.not_supportive_csv,
         name='not_supportive_csv'),
]
