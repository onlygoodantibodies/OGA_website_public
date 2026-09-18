from django.urls import path, include
from django.shortcuts import redirect
from . import mcp_usage, views

urlpatterns = [
    path('', views.home, name='home'),  # Home page
    path('about/', views.about, name='about'),  # About Us page
    path('partners/', views.partners, name='partners'),  # Partners page
    path('roadmap/', views.roadmap, name='roadmap'),
    path('roadmap/institutions/', views.roadmap_institutions, name='roadmap_institutions'),
    path('roadmap/funders/', views.roadmap_funders, name='roadmap_funders'),
    path('roadmap/publishers/', views.roadmap_publishers, name='roadmap_publishers'),
    path('roadmap/manufacturers/', views.roadmap_manufacturers, name='roadmap_manufacturers'),
    path('champions/', views.champions, name='champions'),
    path('contact/', views.contact, name='contact'),  # Contact page
    # Nominate a gene we do not have. It is the home page search box's dead
    # end made into a door: "no match" now offers this, carrying the gene.
    path('nominate/', views.nominate_gene, name='nominate_gene'),
    path('success/', views.success, name='success'),  # Success page
    path('using-the-data/', views.using_the_data, name='using_the_data'),
    path('data-access/', views.data_access, name='data_access'),
    path('data-access/api/', views.api_reference, name='api_reference'),
    path('antibodies/<str:gene_name>/', views.antibody_table, name='antibody_table'),
    path('<int:gene_id>/', views.gene_redirect, name='gene_redirect'),  # Redirect old numeric URLs
    path('projects/', lambda request: redirect('about', permanent=True)),  # Old projects page → about
    path('privacy-policy/', views.privacy_policy, name='privacy_policy'),
    path('api/internal/recommendations/<str:gene_name>/', views.gene_recommendations, name='gene_recommendations'),
    path('api/internal/genes/', views.gene_search_index, name='gene_search_index'),
    path('api/internal/antibodies/', views.antibody_search, name='antibody_search'),
    path('api/', include('core.api_urls')),
    path('embed/', views.embed_antibody_card, name='embed_antibody_card'),
    path('portal/', views.portal, name='portal'),

    # News & Publications (combined page + redirects from old URLs)
    path('news-publications/', views.news_publications, name='news_publications'),
    path('news/', lambda request: redirect('news_publications', permanent=True), name='news'),
    path('publications/', lambda request: redirect('news_publications', permanent=True), name='publications'),

    # Browser extension (install page + the data snapshot it downloads)
    path('extension/', views.extension, name='extension'),
    path('extension/index.json', views.extension_index, name='extension_index'),
    path('extension/citations.json', views.extension_citations, name='extension_citations'),
    path('extension/download/', views.extension_download, name='extension_download'),
    path('extension/download/', views.extension_download, name='extension_download'),
    path('extension/updates.json', views.extension_updates, name='extension_updates'),
    path('extension/firefox.xpi', views.extension_firefox_xpi, name='extension_firefox_xpi'),

    # The hosted MCP server reports each tool call here, so usage survives the
    # seven days Render keeps that service's log (`core/mcp_usage.py`). Token-
    # gated and 404 when no token is set, which is why it can sit in the public
    # URLconf.
    path('internal/mcp-usage/', mcp_usage.report, name='mcp_usage_report'),

    # Validation tools
    path('tools/', views.tools_hub, name='tools_hub'),
    path('tools/connect-your-ai/', views.connect_your_ai, name='connect_your_ai'),
    # Renamed 22 Aug 2026. The page is the MCP connector — "connect your AI to
    # the OGA database" — and never was the Academy's conversational tutor, which
    # lives at academy:assistant. The old address is in Google's index, in the
    # shipped browser extension's handoff card, and in whatever anybody has
    # bookmarked, so it stays as a permanent redirect. It DELIBERATELY carries no
    # url name: with one name per page, no template can link to the old address.
    path('tools/ai-tutor/',
         lambda request: redirect('connect_your_ai', permanent=True)),
    # The old validation planner is replaced by the Selection Tool (selector app);
    # keep the URL + name working by permanently redirecting it there.
    path('tools/validation-planner/', lambda request: redirect('selector:tool', permanent=True), name='validation_planner'),
    path('tools/validation-record/', views.validation_record, name='validation_record'),
    # The Manuscript Validation Checker was retired on 21 Aug 2026 — the browser
    # extension and the MCP connector answer the same question (which antibodies
    # in this paper are independently knockout-tested) without the manual data
    # entry, and both are on the Tools hub. In 30 days of live logs the page was
    # fetched ~30 times, almost all crawlers, and the submit and RRID-lookup
    # endpoints were never called once — nobody ever completed a record.
    #
    # The address is kept as a permanent redirect because it was in Google's
    # index, and DELIBERATELY carries no url name: with no name, no template can
    # link to a tool that no longer exists. The two POST/JSON endpoints under it
    # are simply gone — a 404 is the right answer for an API that was withdrawn.
    path('tools/validation-recorder/',
         lambda request: redirect('tools_hub', permanent=True)),
    path('tools/validation-framework/', views.validation_framework, name='validation_framework'),

    # Recommendation manager — moved onto the pipeline (behind pipeline-member
    # login) at pipeline:recommendations. Keep the old address working.
    path('admin-tools/recommendations/', lambda request: redirect('pipeline:recommendations'), name='admin_recommendations'),
]