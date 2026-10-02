"""
Pipeline URL configuration.

DO NOT MODIFY in parallel chats — changes here are coordinated in the main chat.

All pipeline views live under /pipeline/ (configured in OGA_website/urls.py).
"""

from django.urls import path
from django.views.generic import RedirectView

from pipeline import views

app_name = 'pipeline'

urlpatterns = [
    # === Dashboard (Priority 1) ===
    path('login/', views.pipeline_login, name='pipeline_login'),
    path('start/', views.hub, name='hub'),
    # The end-to-end walkthrough — one gene through the whole pipeline, in
    # screenshots taken from a real run. `/guide/` rather than `/walkthrough/`
    # because it is the thing somebody types when they are stuck.
    path('guide/', views.walkthrough, name='walkthrough'),
    # One search box for the whole pipeline. An exact gene redirects to that
    # gene's page; anything else lands on a page saying where it appears.
    path('find/', views.find, name='find'),
    # The nine pages the boards replaced are gone, not hidden: a page that still
    # answers its URL is a page people still land on, and both field tests did.
    # Retired 31 Jul 2026 — antibody_search, cell_line_list, session_list,
    # session_data, session_plan, target_add, antibody_add, and the standalone
    # bulk_antibodies / bulk_cell_lines paste pages. Their parse and commit
    # endpoints stay: the boards' pop-outs post to them.
    #
    # `dashboard` keeps its name and its redirect. Bookmarks and old links point
    # at it, and it is reversed from templates.
    path('', RedirectView.as_view(pattern_name='pipeline:target_board',
                                  permanent=False, query_string=True),
         name='dashboard'),
    path('target/<int:pk>/', views.target_detail, name='target_detail'),
    path('target/<int:pk>/generate-report/', views.generate_report_view, name='generate_report'),
    path('overview/', views.master_dashboard, name='master_dashboard'),
    path('logout/', views.pipeline_logout, name='pipeline_logout'),

    # === Antibody + cell line files ===
    # The detail and edit pages went on 31 Jul 2026. They survived the first
    # retirement only because they were the one place identity — catalogue,
    # supplier, gene; name, genotype, parent — could be changed; that is the
    # boards' identity dialog now, which shows what would follow the row before
    # you change it. Nothing else on them was unique.
    path('antibodies/export/', views.antibody_export, name='antibody_export'),
    path('antibodies/bulk/parse/', views.bulk_antibodies_parse, name='bulk_antibodies_parse'),
    path('antibodies/bulk/commit/', views.bulk_antibodies_commit, name='bulk_antibodies_commit'),
    path('cell-lines/export/', views.cell_line_export, name='cell_line_export'),
    path('cell-lines/bulk/parse/', views.bulk_cell_lines_parse, name='bulk_cell_lines_parse'),
    path('cell-lines/bulk/commit/', views.bulk_cell_lines_commit, name='bulk_cell_lines_commit'),
    path('import/template/<str:kind>/', views.import_template, name='import_template'),
    path('import/upload/<str:kind>/', views.import_upload, name='import_upload'),

    # === Downloads & uploads (whole-dataset file round-trip, members only) ===
    path('records/delete/preview/', views.delete_preview, name='delete_preview'),
    path('records/delete/', views.delete_commit, name='delete_commit'),
    path('data/', views.data_io, name='data_io'),
    path('data/snapshot/', views.dataset_snapshot_download, name='dataset_snapshot_download'),
    path('data/export/', views.dataset_export, name='dataset_export'),
    path('data/upload/preview/', views.dataset_upload_preview, name='dataset_upload_preview'),
    path('data/upload/commit/', views.dataset_upload_commit, name='dataset_upload_commit'),

    # === Feasibility (Priority 2) ===
    path('feasibility/', views.feasibility, name='feasibility'),
    path('feasibility/lookup/', views.feasibility_lookup, name='feasibility_lookup'),
    path('feasibility/antibodies/', views.feasibility_antibodies, name='feasibility_antibodies'),
    path('feasibility/add/', views.feasibility_add_target, name='feasibility_add_target'),
    # Preview then create. The preview does the UniProt lookups under a deadline
    # and the commit does none, so a slow API costs a retry rather than a
    # half-written batch — see `services/bulk_targets.py`.
    path('feasibility/bulk-check/', views.feasibility_bulk_check, name='feasibility_bulk_check'),
    path('feasibility/bulk-add/', views.feasibility_bulk_add, name='feasibility_bulk_add'),

    # === Cell lines board (find + edit in place) ===
    path('cell-lines/board/', views.cell_line_board, name='cell_line_board'),
    path('cell-lines/board/rows/', views.cell_line_board_rows, name='cell_line_board_rows'),
    path('cell-lines/board/patch/', views.cell_line_board_patch, name='cell_line_board_patch'),
    path('cell-lines/board/add-batch/', views.cell_line_add_batch, name='cell_line_add_batch'),
    path('cell-lines/board/identity/', views.cell_line_identity, name='cell_line_identity'),
    path('cell-lines/board/identity/save/', views.cell_line_identity_save, name='cell_line_identity_save'),
    path('cell-lines/guide/', views.cell_line_board_guide, name='cell_line_board_guide'),

    # === Antibodies board (find + edit in place) ===
    path('antibodies/board/', views.antibody_board, name='antibody_board'),
    path('antibodies/board/rows/', views.antibody_board_rows, name='antibody_board_rows'),
    path('antibodies/board/patch/', views.antibody_board_patch, name='antibody_board_patch'),
    # Renumbering is a whole-mapping operation, never a cell — see
    # services/renumber.py. Two endpoints, because the preview must be
    # readable before anything moves.
    path('antibodies/board/renumber/', views.antibody_renumber_plan,
         name='antibody_renumber_plan'),
    path('antibodies/board/renumber/apply/', views.antibody_renumber_apply,
         name='antibody_renumber_apply'),
    # Identity — catalogue, supplier, gene — is not an inline cell, because
    # retyping one in a grid makes the row a different antibody while every
    # result stays attached. It is a deliberate change that shows what would
    # follow first. Replaces the antibody edit page.
    path('antibodies/board/identity/', views.antibody_identity, name='antibody_identity'),
    path('antibodies/board/identity/save/', views.antibody_identity_save, name='antibody_identity_save'),
    path('antibodies/guide/', views.antibody_board_guide, name='antibody_board_guide'),

    # === Sessions board (find + edit in place; same shape as the target board) ===
    path('sessions/board/', views.session_board, name='session_board'),
    path('sessions/board/rows/', views.session_board_rows, name='session_board_rows'),
    path('sessions/board/line-options/', views.session_board_line_options,
         name='session_board_line_options'),
    path('sessions/board/results/', views.session_board_results,
         name='session_board_results'),
    path('sessions/board/patch/', views.session_board_patch, name='session_board_patch'),
    path('sessions/board/export/', views.session_board_export, name='session_board_export'),
    path('sessions/board/upload/preview/', views.session_board_upload_preview,
         name='session_board_upload_preview'),
    path('sessions/board/upload/commit/', views.session_board_upload_commit,
         name='session_board_upload_commit'),
    path('sessions/guide/', views.session_board_guide, name='session_board_guide'),

    # === Sessions (Priority 2) ===
    path('session/new/', views.session_create, name='session_create'),
    # Per-gene, tab-per-application pre-filled template (download) + its upload
    path('session/template/', views.session_template_export, name='session_template_export'),
    path('session/template/upload/preview/', views.session_template_upload_preview, name='session_template_upload_preview'),
    path('session/template/upload/commit/', views.session_template_upload_commit, name='session_template_upload_commit'),
    # The session_plan *page* is retired; these two are not. The sessions board's
    # new-entry pop-out posts to them, so they are the board's write path now.
    path('session/plan/parse/', views.session_plan_parse, name='session_plan_parse'),
    path('session/plan/commit/', views.session_plan_commit, name='session_plan_commit'),
    path('session/<int:pk>/results/upload/', views.session_results_upload, name='session_results_upload'),
    path('session/<int:pk>/results/commit/', views.session_results_commit, name='session_results_commit'),
    # The session page is retired: its results grid, conditions, protocol phases
    # and bench-sheet round trip are all on the sessions board. The URL survives
    # as a redirect because the first field test recorded every result here, so
    # the bookmarks and pasted links exist and a 404 would strand them.
    path('session/<int:pk>/', views.session_detail, name='session_detail'),
    path('session/<int:pk>/download/<str:artifact>/', views.session_entry.session_download, name='session_download'),

    # Raw files against a session — the gel scan, the Ponceau, the .fcs. The
    # download is served here rather than from storage so it can be gated and so
    # the page can name the file that arrived; see views/attachments.py.
    path('session/attachments/', views.attachment_list, name='attachment_list'),
    path('session/attachments/upload/', views.attachment_upload, name='attachment_upload'),
    path('session/attachments/delete/', views.attachment_delete, name='attachment_delete'),
    path('session/attachments/<int:pk>/download/', views.attachment_download,
         name='attachment_download'),

    # Depositing a gene's data to Zenodo. The preview makes no network call,
    # so it answers before any token exists; the submit hands a draft to the
    # ycharos community for review and never publishes.
    path('target/<int:pk>/deposit/preview/', views.deposit_preview,
         name='deposit_preview'),
    path('target/<int:pk>/deposit/submit/', views.deposit_submit,
         name='deposit_submit'),

    # Reagent batches were retired on 31 Jul 2026 — ordering goes back to a
    # person for now. The app could email a manufacturer contact directly, and
    # that is not a conversation to automate before the partners expect it. The
    # models stay and the whole-dataset sheet still carries `reagent_requests`,
    # so the rows are intact; nothing reaches the ordering workflow. See
    # tests_retired_pages.py.

    # === Figure cropper (grid → crops → DB/R2) ===
    path('cropper/', views.cropper, name='cropper'),
    path('cropper/gene-status/', views.cropper_gene_status, name='cropper_gene_status'),
    path('cropper/ocr/', views.cropper_ocr, name='cropper_ocr'),
    path('cropper/parse-metadata/', views.cropper_parse_metadata, name='cropper_parse_metadata'),
    path('cropper/stage-image/', views.cropper_stage_image, name='cropper_stage_image'),
    path('cropper/discard-image/', views.cropper_discard_image, name='cropper_discard_image'),
    path('cropper/image/<int:pk>/', views.cropper_image, name='cropper_image'),
    path('cropper/session/save/', views.cropper_session_save, name='cropper_session_save'),
    path('cropper/session/load/', views.cropper_session_load, name='cropper_session_load'),
    path('cropper/session/list/', views.cropper_session_list, name='cropper_session_list'),
    path('cropper/session/delete/', views.cropper_session_delete, name='cropper_session_delete'),
    path('cropper/storage/', views.cropper_storage_manifest, name='cropper_storage_manifest'),
    path('cropper/storage/clear/', views.cropper_storage_clear, name='cropper_storage_clear'),
    path('cropper/commit/', views.cropper_commit, name='cropper_commit'),

    # === Review queue (crops wait here; releasing is what publishes them) ===
    path('review/', views.review_queue, name='review_queue'),
    path('review/rows/', views.review_rows, name='review_rows'),
    path('review/image/<int:pk>/', views.review_image, name='review_image'),
    path('review/release/', views.review_release, name='review_release'),
    path('review/recommend/', views.review_recommend, name='review_recommend'),
    path('review/discard/', views.review_discard, name='review_discard'),
    path('review/judge/', views.review_judge, name='review_judge'),
    # Whole IHC figures for a gene's IHC page: private until released, so the
    # queue serves their bytes itself; the consent panel asks the server what
    # the *chosen* set would do; a released one can be withdrawn on its own.
    path('review/ihc-image/<int:pk>/', views.review_ihc_image, name='review_ihc_image'),
    path('review/manifest/', views.review_manifest, name='review_manifest'),
    path('review/ihc-withdraw/', views.review_ihc_withdraw, name='review_ihc_withdraw'),
    # Replacing one antibody's figure with a finished panel: stages into the
    # same review queue, so releasing is still what publishes it.
    # A gene's published methods, typed from its report — what the Copy
    # methods button prints (services/methods_entry.py).
    path('methods/', views.methods_entry, name='methods_entry'),
    path('methods/state/', views.methods_entry_state, name='methods_entry_state'),
    path('methods/preview/', views.methods_entry_preview, name='methods_entry_preview'),
    path('methods/save/', views.methods_entry_save, name='methods_entry_save'),
    path('figures/replace/', views.figure_replace, name='figure_replace'),
    path('figures/replace/upload/', views.figure_replace_upload,
         name='figure_replace_upload'),

    # === Target board (cross-site master target list; replaces Carl's Excel) ===
    path('targets/board/', views.target_board, name='target_board'),
    path('targets/board/rows/', views.target_board_rows, name='target_board_rows'),
    path('targets/board/patch/', views.target_board_patch, name='target_board_patch'),
    path('targets/board/export/', views.target_board_export, name='target_board_export'),
    path('targets/board/upload/preview/', views.target_board_upload_preview,
         name='target_board_upload_preview'),
    path('targets/board/upload/commit/', views.target_board_upload_commit,
         name='target_board_upload_commit'),
    path('targets/guide/', views.target_board_guide, name='target_board_guide'),
    path('targets/check/', views.nomination_check, name='nomination_check'),
    path('targets/portfolio/', views.target_portfolio, name='target_portfolio'),
    # Genes the public asked for on the website — a different list from the
    # consortium's own nominations, and deliberately read-only. See
    # pipeline/gene_request_models.py.
    path('gene-requests/', views.gene_request_board, name='gene_requests'),
    # The Academy's modules, editable by any member (they were admin-only).
    # The rows are in academy_db; see academy/editing.py.
    path('academy/', views.academy_modules, name='academy_modules'),
    path('academy/<int:pk>/', views.academy_module_edit, name='academy_module_edit'),
    path('academy/<int:pk>/history/', views.academy_module_history,
         name='academy_module_history'),
    path('academy/<int:pk>/history/<int:rev>/', views.academy_module_version,
         name='academy_module_version'),
    path('impact/', views.impact_dashboard, name='impact'),
    # One organisation's API activity, reached by clicking its name on the
    # impact page. A detail page rather than a Browse destination, the same
    # shape as `target_detail`: arriving here means somebody picked a row.
    path('impact/consumer/<int:pk>/', views.api_consumer, name='api_consumer'),
    # Who at each manufacturer is sent their results, and resending the
    # summary. Superusers only (`core/manufacturer_contacts.py`).
    path('manufacturer-emails/', views.manufacturer_emails, name='manufacturer_emails'),
    path('manufacturer-emails/action/', views.manufacturer_emails_action,
         name='manufacturer_emails_action'),

    # === People board — logins + pipeline access. Superusers only. ===
    # The fifth board. It exists because a working login is three rows in two
    # databases and nothing showed all three at once: a password changed in
    # Django admin reported success and changed nothing, because that form's
    # password field is a read-only hash behind a separate button.
    path('users/board/', views.user_board, name='user_board'),
    path('users/board/rows/', views.user_board_rows, name='user_board_rows'),
    path('users/board/patch/', views.user_board_patch, name='user_board_patch'),
    path('users/board/check/', views.user_board_check, name='user_board_check'),
    path('users/board/commit/', views.user_board_commit, name='user_board_commit'),
    path('users/board/password/', views.user_reset_password, name='user_reset_password'),

    # Set recommendations retired on 25 Sep 2026: Judge outcomes sets every
    # recommendation (FC included) beside the judgement it rests on, and holds
    # the withdraw button. The name and a redirect stay, carrying `?gene=`,
    # because people bookmarked it.
    path('recommendations/',
         RedirectView.as_view(pattern_name='pipeline:outcomes',
                              permanent=False, query_string=True),
         name='recommendations'),

    # === Judge outcomes (two-axis verdict behind the recommendation) ===
    path('outcomes/', views.outcomes, name='outcomes'),
    path('outcomes/genes/', views.outcome_genes, name='outcome_genes'),
    path('outcomes/antibodies/', views.outcome_antibodies, name='outcome_antibodies'),
    path('outcomes/save/', views.outcome_save, name='outcome_save'),
    path('outcomes/history/', views.outcome_history,
         name='outcome_history'),
    path('outcomes/recommend/', views.outcome_recommend,
         name='outcome_recommend'),
    path('outcomes/withdraw/', views.outcome_withdraw,
         name='outcome_withdraw'),
    ]