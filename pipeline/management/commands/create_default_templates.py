"""
Management command: create_default_templates

Creates 7 protocol templates with conditions and collapsible guidance
drawn from Nature Protocols (s41596-024-01095-8), SYT1 F1000 (Leicester FC),
SERPINA1 methods (Leicester WB/IP/FC), and protocols.io step-by-steps.

Usage:
    python manage.py create_default_templates --database=pipeline_db

Re-runnable: uses update_or_create on (name, site, procedure_type).
"""

from django.core.management.base import BaseCommand
from pipeline.models import ProtocolTemplate, Site

DB = 'pipeline_db'


class Command(BaseCommand):
    help = 'Create default protocol templates from published YCharOS methods'

    def handle(self, *args, **options):
        # Get sites
        montreal = Site.objects.using(DB).get(name__icontains='Montreal')
        leicester = Site.objects.using(DB).get(name__icontains='Leicester')

        templates = self._build_templates(montreal, leicester)

        created = 0
        updated = 0
        for t in templates:
            _, was_created = ProtocolTemplate.objects.using(DB).update_or_create(
                name=t['name'],
                site_id=t['site'].pk,
                procedure_type=t['procedure_type'],
                defaults={
                    'conditions': t['conditions'],
                    'description': t['description'],
                    'is_default': t.get('is_default', False),
                    'protocol_guidance': t['protocol_guidance'],
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done: {created} created, {updated} updated'
        ))

    def _build_templates(self, montreal, leicester):
        return [
            self._montreal_wb_standard(montreal),
            self._leicester_wb_standard(leicester),
            self._leicester_wb_secreted(leicester),
            self._montreal_ip_standard(montreal),
            self._leicester_ip_standard(leicester),
            self._montreal_if_standard(montreal),
            self._leicester_fc_standard(leicester),
        ]

    # ─────────────────────────────────────────────────────────
    # 1. Montreal WB Standard (intracellular)
    # ─────────────────────────────────────────────────────────
    def _montreal_wb_standard(self, site):
        return {
            'name': 'Montreal WB Standard',
            'site': site,
            'procedure_type': 'WB',
            'is_default': True,
            'description': (
                'Standard western blot protocol for intracellular proteins. '
                'From Nature Protocols (Ayoubi et al. 2025) Procedure 1, Option A.'
            ),
            'conditions': {
                'lysis_buffer': 'RIPA + 1x protease inhibitor cocktail',
                'protein_loading_ug': '20-50',
                'gel_chemistry': '4-20% Tris-Glycine midi (12-well)',
                'transfer_method': 'Wet transfer, 85V, 45 min',
                'blocking': '5% milk in 1x TBST',
                'secondary_antibody': 'HRP-conjugated (Proteintech RGAR001/RGAM001)',
                'secondary_dilution': '1:10000 (0.1 µg/mL)',
                'ecl_type': 'Pierce ECL (1 min incubation)',
                'imaging_system': 'iBright',
                'num_gels': '',
            },
            'protocol_guidance': [
                {
                    'title': 'Sample Preparation',
                    'summary': 'Lyse cells, sonicate, quantify protein by BCA, equalise WT and KO concentrations.',
                    'detail': (
                        'Grow 2x 150mm dishes each of WT and KO cells to 80% confluence. '
                        'Wash 3x with ice-cold PBS. Add 1.0 mL RIPA + protease inhibitors per dish. '
                        'Scrape, pool same-condition lysates, sonicate 3x 5s at 40% amplitude. '
                        'Rock 30 min at 4°C. Centrifuge ~110,000g 15 min 4°C (or 15,000-20,000g 1h if no ultracentrifuge). '
                        'Quantify by BCA in triplicate. '
                        'Protein loading depends on target abundance: check PAXdb — '
                        '50 µg for low abundance (<1000 ppm), 20 µg for high abundance (>1000 ppm). '
                        'Prepare master mixes at equal concentration in 1x Laemmli sample buffer. '
                        'Heat at 65°C 10 min (do NOT boil — can cause artefacts for some proteins). '
                        'PAUSE POINT: lysate aliquots can be stored at -20°C (6 months) or -80°C (1 year).'
                    ),
                    'data_fields': ['protein_loading_ug'],
                },
                {
                    'title': 'Gel Loading & Electrophoresis',
                    'summary': 'Load ladder + WT + KO per antibody. 4 antibodies per 12-well gel. Run until dye front ~3mm from bottom.',
                    'detail': (
                        'Use 4-20% TG gels for proteins 26-500 kDa (most targets). '
                        'Use 10% BT gels for proteins 3.5-25 kDa. '
                        '12-well midi gels hold up to 45 µL per well. '
                        'Loading order per gel: [MWM | WT | KO | WT | KO | WT | KO | WT | KO | — | — | MWM]. '
                        'That is 4 antibodies per gel. For N antibodies you need ceil(N/4) gels. '
                        'Run in TG SDS buffer at conditions per gel manufacturer (typically 200V until dye front). '
                        'Running buffer: 100 mL TG SDS 10x + 900 mL water.'
                    ),
                    'data_fields': ['gel_chemistry', 'num_gels'],
                },
                {
                    'title': 'Transfer & Ponceau',
                    'summary': 'Wet transfer to nitrocellulose, Ponceau stain, scan, cut membrane strips per antibody.',
                    'detail': (
                        'Transfer to 0.45 µm nitrocellulose using Bio-Rad Criterion blotter. '
                        'TG gels: TG transfer buffer + 20% methanol, 85V 45 min. '
                        'BT gels: use manufacturer-specified transfer conditions. '
                        'Wash membrane 2x in deionised water. '
                        'Stain with Ponceau S for 1 min, wash excess with water until bands visible. '
                        'Dry on Whatman filter paper. '
                        'Label each lane with catalogue number and host species using smudge-proof pen. '
                        'Scan Ponceau-stained membranes — these appear in the published figures as loading controls. '
                        'Cut membrane into strips: each strip = MWM + WT + KO for one antibody. '
                        'PAUSE POINT: dried membranes can be stored at RT for months.'
                    ),
                    'data_fields': ['transfer_method'],
                },
                {
                    'title': 'Primary Antibody (overnight)',
                    'summary': 'Rehydrate strips, block 1h in 5% milk, incubate with primary antibody overnight at 4°C.',
                    'detail': (
                        'Rehydrate membrane strips in 1x TBST for 5 min. '
                        'Block in 5% nonfat milk in TBST for 1 hour at RT. '
                        'Prepare primary antibody dilution in 5% milk/TBST per supplier recommendation. '
                        'If supplier does not recommend WB, start at 1 µg/mL. '
                        'Incubate each strip in antibody solution (container or sealed bag). '
                        'Rock overnight at 4°C. '
                        'TROUBLESHOOTING: If signal undetectable after 20 min exposure, increase concentration 5-fold. '
                        'If signal saturated at 1s exposure, decrease concentration 5-fold.'
                    ),
                    'data_fields': ['blocking'],
                },
                {
                    'title': 'Secondary & Imaging',
                    'summary': 'Wash 3x10min, secondary 1h RT, wash 3x10min, ECL, image.',
                    'detail': (
                        'Wash strips 3x 10 min in TBST with rocking. '
                        'Dilute HRP-conjugated secondary to 0.1 µg/mL in 5% milk/TBST. '
                        'Use anti-rabbit (RGAR001) for rabbit primaries, anti-mouse (RGAM001) for mouse. '
                        'Incubate 1 hour at RT. Wash 3x 10 min in TBST. '
                        'Apply Pierce ECL substrate for 1 min, remove excess. '
                        'Image on iBright. Record exposure time for each blot — this goes in the figure legend. '
                        'For weak signals: try Clarity ECL (5 min incubation, higher sensitivity). '
                        'Expected band: check predicted MW on UniProt. '
                        'A specific antibody shows band at predicted MW in WT, absent in KO.'
                    ),
                    'data_fields': ['secondary_antibody', 'secondary_dilution', 'ecl_type', 'imaging_system'],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 2. Leicester WB Standard (intracellular)
    # ─────────────────────────────────────────────────────────
    def _leicester_wb_standard(self, site):
        return {
            'name': 'Leicester WB Standard',
            'site': site,
            'procedure_type': 'WB',
            'is_default': True,
            'description': (
                'Leicester western blot protocol for intracellular proteins. '
                'Based on SERPINA1 methods (Biddle et al.) and SYT1 report.'
            ),
            'conditions': {
                'lysis_buffer': 'RIPA + 1x protease inhibitor cocktail + sodium orthovanadate + PMSF',
                'protein_loading_ug': '30',
                'gel_chemistry': '4-20% WedgeWell Tris-Glycine Plus midi',
                'transfer_method': 'Wet transfer (Criterion blotter), 85V, 45 min',
                'blocking': '5% milk in 1x TBST',
                'secondary_antibody': 'HRP-conjugated (Proteintech RGAR001/RGAM001)',
                'secondary_dilution': '1:10000 (0.1 µg/mL)',
                'ecl_type': 'Pierce ECL or Clarity Western ECL',
                'imaging_system': 'ImageQuant LAS 4000',
                'num_gels': '',
            },
            'protocol_guidance': [
                {
                    'title': 'Sample Preparation',
                    'summary': 'RIPA lysis with protease/phosphatase inhibitors, sonicate 3x5s, BCA quantification.',
                    'detail': (
                        'Wash cells 3x in PBS. Lyse in RIPA + 1x protease inhibitor cocktail + '
                        'sodium orthovanadate + PMSF. Sonicate 3x 5s at 40% amplitude. '
                        'Rock 30 min at 4°C. Centrifuge 20,000g 1h at 4°C. '
                        'Quantify by BCA (Pierce). Use 30 µg protein per lane. '
                        'Prepare in Laemmli + 2-mercaptoethanol (355 mM final). '
                        'Heat 65°C 10 min.'
                    ),
                    'data_fields': ['protein_loading_ug'],
                },
                {
                    'title': 'Gel Loading & Electrophoresis',
                    'summary': 'WedgeWell 4-20% TG Plus midi gels, 200V for 1 hour in TG SDS buffer.',
                    'detail': (
                        'Load samples + Prime-Step prestained broad range ladder (BioLegend 773302). '
                        'Run in SureLock Tandem Midi Gel tanks at 200V for 1 hour. '
                        'Running buffer: Tris/Glycine/SDS (Bio-Rad 1610772). '
                        'Same loading pattern as Montreal: MWM + WT/KO pairs, 4 antibodies per 12-well gel.'
                    ),
                    'data_fields': ['gel_chemistry', 'num_gels'],
                },
                {
                    'title': 'Transfer & Ponceau',
                    'summary': 'Wet transfer to 0.2µm nitrocellulose, 85V 45min, Ponceau S, scan.',
                    'detail': (
                        'Transfer to 0.2 µm supported nitrocellulose (Cytiva 10600015). '
                        'Criterion blotter with plate electrodes (Bio-Rad 17004070), 85V 45 min. '
                        'Ponceau S stain (Thermo Fisher 161470250), scan alongside blots. '
                        'Cut strips per antibody. Label with catalogue number + host species.'
                    ),
                    'data_fields': ['transfer_method'],
                },
                {
                    'title': 'Primary Antibody (overnight)',
                    'summary': 'Block 1h in 5% milk/TBST, primary antibody overnight at 4°C.',
                    'detail': (
                        'Block 1 hour in 5% milk/TBST. Prepare primary in 5% milk/TBST '
                        'per supplier recommendation. Rock overnight at 4°C.'
                    ),
                    'data_fields': ['blocking'],
                },
                {
                    'title': 'Secondary & Imaging',
                    'summary': 'Wash 3x10min, secondary 1h, ECL, ImageQuant LAS 4000.',
                    'detail': (
                        'Wash 3x 10 min TBST. Secondary HRP at 0.1 µg/mL in 5% milk/TBST, '
                        '1 hour RT. Wash 3x 10 min. '
                        'Apply Pierce ECL for 1 min (or Clarity ECL for 5 min for weaker signals). '
                        'Image on ImageQuant LAS 4000. Record exposure time.'
                    ),
                    'data_fields': ['secondary_antibody', 'secondary_dilution', 'ecl_type', 'imaging_system'],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 3. Leicester WB Secreted (culture medium collection)
    # ─────────────────────────────────────────────────────────
    def _leicester_wb_secreted(self, site):
        return {
            'name': 'Leicester WB Secreted Protein',
            'site': site,
            'procedure_type': 'WB',
            'is_default': False,
            'description': (
                'Western blot protocol for secreted proteins via culture medium collection. '
                'From SERPINA1 methods — serum deprivation, concentration, then standard WB. '
                'Use this instead of Leicester WB Standard when target is secreted (check UniProt subcellular location).'
            ),
            'conditions': {
                'lysis_buffer': 'N/A — culture medium collection',
                'protein_loading_ug': '30',
                'gel_chemistry': '4-20% WedgeWell Tris-Glycine Plus midi',
                'transfer_method': 'Wet transfer (Criterion blotter), 85V, 45 min',
                'blocking': '5% milk in 1x TBST',
                'secondary_antibody': 'HRP-conjugated (Proteintech RGAR001/RGAM001)',
                'secondary_dilution': '1:10000 (0.1 µg/mL)',
                'ecl_type': 'Pierce ECL or Clarity Western ECL',
                'imaging_system': 'ImageQuant LAS 4000',
                'num_gels': '',
            },
            'protocol_guidance': [
                {
                    'title': 'Culture Medium Collection',
                    'summary': 'Serum-deprive 48h, collect medium, centrifuge to clear, concentrate with 3 kDa filter.',
                    'detail': (
                        'Wash cells 3x in HBSS. Replace with phenol red-free MEM + 1% sodium pyruvate + '
                        '1% antibiotic-antimycotic + 1% L-glutamine (no serum). '
                        'Incubate 48 hours (SERPINA1 protocol; Nature Protocols standard is 18h — '
                        'extend if target expression is low). '
                        'Collect medium, centrifuge 500g 10 min 4°C, then 4500g 10 min 4°C. '
                        'Concentrate using Amicon Ultra 15 mL 3 kDa filters (Sigma UFC900396), '
                        '4000g 30 min 4°C — yields ~30-fold concentration. '
                        'Add 1x protease inhibitor cocktail. '
                        'PAUSE POINT: concentrated media aliquots can be stored at -20°C for 1 year. '
                        'TROUBLESHOOTING: If concentration too low, use 0.5 mL centrifugal filters for further concentration, '
                        'or extend serum-free incubation (do not exceed 36h to avoid cell stress).'
                    ),
                    'data_fields': [],
                },
                {
                    'title': 'Sample Preparation & BCA',
                    'summary': 'Quantify concentrated medium by BCA, prepare 30 µg samples.',
                    'detail': (
                        'Measure protein concentration by BCA or Bradford in triplicate. '
                        'Dilute WT and KO samples to identical final concentrations using '
                        'the same serum-free medium or deionised water. '
                        'Combine with Laemmli + 2-mercaptoethanol (355 mM final). '
                        'Heat 65°C 10 min. Use 30 µg per lane.'
                    ),
                    'data_fields': ['protein_loading_ug'],
                },
                {
                    'title': 'Electrophoresis, Transfer & Imaging',
                    'summary': 'Same as Leicester WB Standard from this point.',
                    'detail': (
                        'Follow Leicester WB Standard protocol: WedgeWell 4-20% TG Plus gels, '
                        '200V 1h, wet transfer to 0.2 µm nitrocellulose 85V 45 min, '
                        'Ponceau, block, primary O/N, secondary, ECL, ImageQuant LAS 4000.'
                    ),
                    'data_fields': ['gel_chemistry', 'num_gels', 'ecl_type', 'imaging_system'],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 4. Montreal IP Standard
    # ─────────────────────────────────────────────────────────
    def _montreal_ip_standard(self, site):
        return {
            'name': 'Montreal IP Standard',
            'site': site,
            'procedure_type': 'IP',
            'is_default': True,
            'description': (
                'Standard immunoprecipitation protocol. '
                'Nature Protocols Procedure 2. Uses Dynabeads Protein A/G, '
                '2 µg antibody, 1 mg lysate, 1h incubation.'
            ),
            'conditions': {
                'lysis_buffer': 'IP lysis buffer (25 mM Tris-HCl pH 7.4, 150 mM NaCl, 1 mM EDTA, 1% NP-40, 5% glycerol)',
                'bead_type': 'Dynabeads Protein A (rabbit) / Protein G (mouse)',
                'protein_amount_mg': '1.0',
                'detection_antibody': '',
                'detection_antibody_dilution': '',
                'secondary_antibody': 'Veriblot (Abcam ab131366)',
                'secondary_dilution': '1:1000 (0.04 µg/mL)',
                'gel': '4-20% Tris-Glycine midi',
                'membrane': 'Nitrocellulose',
                'ecl': 'Pierce ECL',
                'detection_system': 'iBright',
            },
            'protocol_guidance': [
                {
                    'title': 'Lysate Preparation (use fresh)',
                    'summary': 'IP lysis buffer, sonicate, ultracentrifuge. Fresh lysate required — do not freeze.',
                    'detail': (
                        'Grow 7x 150 mm dishes of WT cells for 12 antibodies (1 dish per 2 IPs). '
                        'Lyse in 1.0 mL IP buffer + protease inhibitors per dish. '
                        'Sonicate 3x 5s at 40%. Rock 30 min 4°C. '
                        'Centrifuge ~110,000g 15 min 4°C. Pool supernatants. '
                        'Adjust to 2.0 mg/mL. '
                        'CRITICAL: Fresh lysates must be used — freezing may affect epitope recognition. '
                        'For secreted proteins: collect and concentrate culture medium instead '
                        '(13x 150 mm dishes for 12 IPs, 0.5 mg at 1.0 mg/mL per IP).'
                    ),
                    'data_fields': ['lysis_buffer', 'protein_amount_mg'],
                },
                {
                    'title': 'Antibody-Bead Conjugation',
                    'summary': '2 µg antibody + 30 µL Dynabeads in IP buffer, rock 1h at 4°C, wash 2x.',
                    'detail': (
                        'Add 2 µg of each antibody to 1 mL IP buffer with 30 µL Protein A (rabbit) '
                        'or Protein G (mouse) Dynabeads. Rock 1h at 4°C. '
                        'Place on DynaMag-2 magnet. Remove supernatant. '
                        'Wash 2x with 1.0 mL IP buffer to remove unbound antibody. '
                        'Do not let beads dry out. '
                        'IP volume calculation: amount = 2 µg. Volume = 2 ÷ concentration (µg/µL). '
                        'The pipeline calculates this automatically from the antibody concentration field. '
                        'TROUBLESHOOTING: If concentration unknown, use 5-10 µL as starting volume.'
                    ),
                    'data_fields': ['bead_type'],
                },
                {
                    'title': 'Immunoprecipitation',
                    'summary': 'Add 1 mg lysate to beads, incubate 1h at 4°C rotating, collect unbound, wash 3x.',
                    'detail': (
                        'Remove buffer from antibody-bead conjugate using magnet. '
                        'Add protein sample: 1.0 mg (500 µL at 2.0 mg/mL) for lysates, '
                        'or 0.5 mg (500 µL at 1.0 mg/mL) for concentrated media. '
                        'Incubate 1h at 4°C with constant rotation. '
                        'Collect 20 µL unbound fraction (=4% UB for gel loading). '
                        'Save 20 µL of starting material before IP (=4% SM). '
                        'Wash beads 3x with 1.0 mL IP buffer. '
                        'Elute with 50 µL 2x sample buffer + reducing agent. Heat 65°C 10 min. '
                        'Load: SM (4%), UB (4%), IP eluate (whole) on gel. '
                        'TROUBLESHOOTING: If no capture, extend incubation to 18h at 4°C.'
                    ),
                    'data_fields': [],
                },
                {
                    'title': 'WB Detection',
                    'summary': 'Run gel, transfer, probe with a validated detection antibody, Veriblot secondary.',
                    'detail': (
                        'Run SM, UB, IP on 4-20% TG gel (3 antibodies per 12-well gel). '
                        'Transfer and probe with a KO-controlled detection antibody — '
                        'ideally a recombinant antibody with high specificity from WB screening. '
                        'Use Veriblot secondary (Abcam ab131366) at 0.04 µg/mL to avoid heavy chain interference. '
                        'A successful IP: target enriched in IP lane, depleted from UB lane. '
                        'Record exposure time and ECL type for figure legend.'
                    ),
                    'data_fields': [
                        'detection_antibody', 'detection_antibody_dilution',
                        'secondary_antibody', 'secondary_dilution',
                        'ecl', 'detection_system',
                    ],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 5. Leicester IP Standard (secreted protein)
    # ─────────────────────────────────────────────────────────
    def _leicester_ip_standard(self, site):
        return {
            'name': 'Leicester IP Standard',
            'site': site,
            'procedure_type': 'IP',
            'is_default': True,
            'description': (
                'Leicester IP protocol. From SERPINA1 methods — uses concentrated '
                'culture medium (secreted proteins). Same bead/antibody principles as Montreal.'
            ),
            'conditions': {
                'lysis_buffer': 'Pierce IP Lysis Buffer (25 mM Tris-HCl pH 7.4, 150 mM NaCl, 1 mM EDTA, 1% NP-40, 5% glycerol)',
                'bead_type': 'Dynabeads Protein A (rabbit) / Protein G (mouse)',
                'protein_amount_mg': '0.5',
                'detection_antibody': '',
                'detection_antibody_dilution': '',
                'secondary_antibody': 'Veriblot (Abcam ab131366)',
                'secondary_dilution': '1:1000 (0.04 µg/mL)',
                'gel': '4-20% Tris-Glycine Plus midi',
                'membrane': 'Nitrocellulose 0.2 µm',
                'ecl': 'Pierce ECL or Clarity',
                'detection_system': 'ImageQuant LAS 4000',
            },
            'protocol_guidance': [
                {
                    'title': 'Culture Medium Collection',
                    'summary': 'Collect conditioned medium from WT cells, centrifuge, concentrate with 3 kDa filter.',
                    'detail': (
                        'Collect serum-free conditioned medium as per Leicester WB Secreted protocol. '
                        'For 12 IPs: need enough concentrated medium for 0.5 mg per IP (6 mg total). '
                        'Adjust concentration to 1.0 mg/mL in IP lysis buffer.'
                    ),
                    'data_fields': ['lysis_buffer', 'protein_amount_mg'],
                },
                {
                    'title': 'Antibody-Bead Conjugation',
                    'summary': '2 µg antibody + 30 µL Dynabeads, rock 1h at 4°C.',
                    'detail': (
                        'Same as Montreal IP Standard. '
                        '2 µg antibody in 1 mL IP buffer + 30 µL Protein A/G beads. '
                        'Rock 1h at 4°C. Wash 2x.'
                    ),
                    'data_fields': ['bead_type'],
                },
                {
                    'title': 'Immunoprecipitation',
                    'summary': '0.5 mg concentrated medium per IP, 18h at 4°C rotating.',
                    'detail': (
                        'Add 0.5 mg (500 µL at 1.0 mg/mL) concentrated medium to beads. '
                        'Incubate 18h at 4°C with constant rotation. '
                        'Collect SM (4%) and UB (4%). Wash 3x. '
                        'Elute in 2x sample buffer + reducing agent, 65°C 10 min. '
                        'Note: Leicester uses longer incubation (18h vs Montreal 1h) for secreted proteins.'
                    ),
                    'data_fields': [],
                },
                {
                    'title': 'WB Detection',
                    'summary': 'Standard WB with Veriblot secondary, ImageQuant LAS 4000.',
                    'detail': (
                        'Run on 4-20% TG Plus gels, transfer to 0.2 µm nitrocellulose. '
                        'Probe with validated detection antibody + Veriblot secondary. '
                        'Image on ImageQuant LAS 4000. Record exposure time and ECL type.'
                    ),
                    'data_fields': [
                        'detection_antibody', 'detection_antibody_dilution',
                        'ecl', 'detection_system',
                    ],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 6. Montreal IF Standard
    # ─────────────────────────────────────────────────────────
    def _montreal_if_standard(self, site):
        return {
            'name': 'Montreal IF Standard',
            'site': site,
            'procedure_type': 'IF',
            'is_default': True,
            'description': (
                'Standard immunofluorescence protocol using WT/KO mosaic in 96-well plates. '
                'Nature Protocols Procedure 3. 4 days total. '
                'Tests each antibody at 2 concentrations with CellTracker labelling.'
            ),
            'conditions': {
                'fixation': '4% PFA + 20% sucrose in 0.5x PBS (37°C, 15 min)',
                'permeabilisation': '0.1% Triton X-100 in PBS (10 min RT)',
                'blocking': '5% BSA + 0.01% Triton X-100 in PBS (30 min RT)',
                'secondary_ab': 'Alexa Fluor 555 (or 568) anti-rabbit/mouse',
                'secondary_condition': '1h RT',
                'dilution_buffer': '5% BSA + 0.01% Triton X-100 in PBS',
                'microscope': 'ImageXpress Micro (widefield high-content)',
                'objective': '20x water Apo LambdaS LWD (NA 0.95)',
            },
            'protocol_guidance': [
                {
                    'title': 'Day 1: WT/KO Mosaic Plating',
                    'summary': 'Coat 96-well plate with poly-L-lysine, label WT/KO with CellTrackers, plate 1:1 mosaic.',
                    'detail': (
                        'Coat 96-well clear flat bottom plate (Revvity 6055300) with 100 µL poly-L-lysine '
                        '(10 µg/mL in borate buffer). Incubate 1h RT. Wash 2x sterile water. '
                        'Trypsinise WT and KO cells from 80% confluent 150mm dishes. '
                        'Label WT with CellTracker Green CMFDA (5 µM), KO with CellTracker Deep Red (1 µM). '
                        'Keep ~100,000 unlabelled cells of each for controls. '
                        'Incubate 30 min at 37°C. Centrifuge, resuspend in complete medium, count. '
                        'Mix WT:KO at 1:1 (10,000 WT + 10,000 KO = 20,000 total per well for most cancer lines). '
                        'Plate in 29 wells per plate layout. Add media only to well 30. '
                        'Incubate overnight at 37°C. '
                        'A total of 30 wells is needed for 12 antibodies at 2 concentrations + controls. '
                        'Standard controls: media only, WT+DAPI, KO+DAPI, DAPI only, secondary only, S6 Ribosomal, ARID1A.'
                    ),
                    'data_fields': [],
                },
                {
                    'title': 'Day 1 (continued): Fixation',
                    'summary': 'Fix with 4% PFA + 20% sucrose, 15 min at 37°C.',
                    'detail': (
                        'Add 100 µL pre-warmed fixation buffer (0.5x PBS + 8% PFA + 20% sucrose) '
                        'on top of culture medium. Incubate 15 min at 37°C. '
                        'Aspirate and wash 3x with 100 µL PBS. '
                        'PAUSE POINT: Fixed plates can be stored at 4°C for a few days (seal with parafilm, protect from light).'
                    ),
                    'data_fields': ['fixation'],
                },
                {
                    'title': 'Day 2: Permeabilisation, Blocking & Primary (overnight)',
                    'summary': 'Permeabilise 10 min, block 30 min, add primary antibody overnight at 4°C.',
                    'detail': (
                        'Permeabilise with 100 µL 0.1% Triton X-100 in PBS for 10 min RT. '
                        'Wash 3x with 100 µL PBS. '
                        'Block with 100 µL IF blocking buffer (5% BSA + 0.01% Triton X-100) for 30 min RT. '
                        'Note: use serum from same species as secondary antibody for blocking if needed. '
                        'Prepare primary antibodies at 2 concentrations per antibody. '
                        'Standard approach: supplier recommended dilution + 2-fold higher concentration. '
                        'If no supplier recommendation: try 1:250 and 1:500 (polyclonal), 1:500 and 1:1000 (monoclonal). '
                        'Incubate overnight at 4°C.'
                    ),
                    'data_fields': ['permeabilisation', 'blocking'],
                },
                {
                    'title': 'Day 3: Secondary, DAPI & Imaging',
                    'summary': 'Wash, secondary antibody 1h, DAPI, image on ImageXpress.',
                    'detail': (
                        'Wash 3x PBS. Incubate with Alexa Fluor 555 secondary + DAPI '
                        '(5 ng/mL in PBS) for 1h RT protected from light. Wash 3x PBS. '
                        'Image: acquire blue (DAPI), green (WT tracker), red (antibody), far-red (KO tracker). '
                        'Filter cube specs: blue 395/25→432/36, green 475/28→520/35, '
                        'red 555/28→600/37, far-red 635/22→692/40. '
                        '20x objective, 16-bit sCMOS. Acquire 3 sites per well.'
                    ),
                    'data_fields': ['secondary_ab', 'secondary_condition', 'microscope', 'objective'],
                },
                {
                    'title': 'Day 4: Image Analysis',
                    'summary': 'Segment cells with Cellpose, quantify per-cell antibody fluorescence in WT vs KO.',
                    'detail': (
                        'Inspect cell mask channels in Fiji. Estimate cell diameter in pixels. '
                        'Run Cellpose segmentation on cell mask images (use "cyto" model). '
                        'GPU recommended for batch processing. '
                        'Output: labelled masks per image. '
                        'Quantify mean fluorescence intensity per cell in the antibody channel. '
                        'Compare WT vs KO distributions. '
                        'A specific antibody: signal in WT cells, KO signal comparable to background (outside cells).'
                    ),
                    'data_fields': [],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────
    # 7. Leicester FC Standard
    # ─────────────────────────────────────────────────────────
    def _leicester_fc_standard(self, site):
        return {
            'name': 'Leicester FC Standard',
            'site': site,
            'procedure_type': 'FC',
            'is_default': True,
            'description': (
                'Flow cytometry protocol. FC is performed at Leicester. '
                'From SYT1 F1000 methods and SERPINA1 methods (Biddle et al.). '
                'CellTracker labelling, PFA fixation, saponin permeabilisation, Attune NxT.'
            ),
            'conditions': {
                'tracker_dyes': 'CellTracker Green CMFDA (WT) / CellTracker Violet (KO)',
                'fixation': '4% PFA in PBS, 20 min on ice',
                'permeabilisation': '0.1% saponin in PBS, 10 min RT',
                'flow_cytometer': 'Attune NxT',
                'analysis_software': 'FlowJo',
                'secondary_ab': 'CoraLite Plus 647 anti-rabbit (RGAR005) / anti-mouse (RGAM005)',
                'secondary_dilution': '0.83 µg/mL (1:600)',
            },
            'protocol_guidance': [
                {
                    'title': 'Cell Labelling',
                    'summary': 'Detach cells, label 5M WT with Green, 5M KO with Violet, combine 1:1.',
                    'detail': (
                        'Detach WT and KO cells. Label 5 million WT cells with CellTracker Green CMFDA, '
                        '5 million KO cells with CellTracker Violet. '
                        'Centrifuge 300g 10 min. Resuspend in 1% BSA/PBS. '
                        'Combine at 1:1 ratio.'
                    ),
                    'data_fields': ['tracker_dyes'],
                },
                {
                    'title': 'Fixation & Permeabilisation',
                    'summary': 'Fix in 4% PFA 20 min on ice, permeabilise in 0.1% saponin 10 min RT, block.',
                    'detail': (
                        'Fix combined cells in 800 µL 4% PFA/PBS for 20 min on ice. '
                        'Add 1.2 mL 1% BSA/PBS, vortex, centrifuge 600g 15 min 4°C. '
                        'Permeabilise in 400 µL 0.1% saponin/PBS for 10 min RT. '
                        'Centrifuge 600g 15 min 4°C. '
                        'Block in 5% goat serum + 1% BSA + 0.1% saponin/PBS for 30 min on ice.'
                    ),
                    'data_fields': ['fixation', 'permeabilisation'],
                },
                {
                    'title': 'Primary & Secondary Antibody',
                    'summary': 'Aliquot 400K cells/tube, primary 30 min ice, wash, secondary 30 min ice.',
                    'detail': (
                        'Aliquot 400,000 cells per labelled tube. Centrifuge 600g 15 min 4°C. '
                        'Incubate in 150 µL primary antibody in 1% BSA + 0.1% saponin/PBS for 30 min on ice. '
                        'All primaries at 1 µg/mL unless concentration unknown (then use 1:100). '
                        'Add 500 µL wash buffer, vortex, centrifuge 600g 15 min 4°C. '
                        'Incubate with CoraLite Plus 647 secondary at 0.83 µg/mL in '
                        '150 µL 1% BSA + 0.1% saponin/PBS for 30 min on ice. '
                        'Wash with 500 µL buffer, centrifuge 600g 15 min 4°C. '
                        'Resuspend in 1 mL 1% BSA/PBS.'
                    ),
                    'data_fields': ['secondary_ab', 'secondary_dilution'],
                },
                {
                    'title': 'Acquisition & Analysis',
                    'summary': 'Acquire on Attune NxT. Gate: FSC/SSC → singlets → WT/KO by tracker. Analyse in FlowJo.',
                    'detail': (
                        'Acquire on Attune NxT flow cytometer. '
                        'Gating strategy: '
                        '1) FSC-A vs SSC-A → cell population. '
                        '2) FSC-A vs FSC-H → single cells. '
                        '3) BL1-A vs VL1-A → separate WT (green) and KO (violet) using quadrant gate. '
                        '4) Quantify antibody staining in RL1-A channel. '
                        'Merge WT and KO histograms to show staining intensity difference. '
                        'Include secondary-only controls for both WT and KO. '
                        'A specific antibody: clear histogram shift between WT and KO populations. '
                        'Assemble figure in Adobe Illustrator.'
                    ),
                    'data_fields': ['flow_cytometer', 'analysis_software'],
                },
            ],
        }
