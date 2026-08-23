/* The site's search box — one implementation, mounted twice.
 *
 * There are two boxes: the homepage hero, over a grid of gene cards, and the
 * slim one in the header bar on every other page. They had a script each, and
 * the moment they answer differently the reader has no way to tell which is
 * broken — so the behaviour lives here and each page supplies only its markup,
 * its class names and what Enter should do when nothing is selected.
 *
 * Two sources, deliberately:
 *
 *   Genes come from a list fetched once (`genesUrl`) and matched in the browser.
 *   It is a few hundred short rows, so suggestions appear as you type with no
 *   round trip, and the homepage already has the same list in its DOM.
 *
 *   Antibodies are asked of the server (`antibodiesUrl`). Matching a catalogue
 *   number means undoing the typesetting a publisher or a supplier put on it —
 *   `14,060-1-AP`, `#ab74140`, `MA511154` — and those rules already exist twice
 *   over in `mcp_servers/common/manuscript.py` and `matcher.js`. A third copy
 *   here is the one that would drift, so the server answers instead.
 *
 * The two arrive at different times, so a render draws whatever it has: genes
 * immediately, antibodies when they land. Every antibody response carries the
 * query it was for and is dropped if the box has moved on — the same rule
 * `board.js::replaceRow` holds, and for the same reason. Without it a slow
 * response for `146` repaints over the results for `14060-1-AP`.
 */
window.OGASearch = (function () {
    'use strict';

    function debounce(fn, delay) {
        var timer;
        return function () {
            var args = arguments, self = this;
            clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(self, args); }, delay);
        };
    }

    function create(cfg) {
        var input = cfg.input;
        var list = cfg.list;
        if (!input || !list || input.dataset.ogaSearchBound) { return null; }
        input.dataset.ogaSearchBound = 'true';

        var itemClass = cfg.itemClass || 'oga-search-item';
        var hintClass = cfg.hintClass || 'oga-search-hint';
        var emptyClass = cfg.emptyClass || 'oga-search-empty';

        /* Escape every parent's overflow and transform context. Both hosts sit
           inside something that clips: the header bar sets overflow on small
           screens, and the homepage hero has a transformed pseudo-element. */
        document.body.appendChild(list);

        var genes = null;
        var genesPromise = null;
        var antibodies = [];
        var antibodiesFor = null;   // the query `antibodies` is an answer to
        var activeIndex = -1;

        function geneUrl(name) {
            return cfg.geneUrlTemplate.replace('OGA_GENE', encodeURIComponent(name));
        }

        /* One fetch, shared by every keystroke. Handing an in-flight caller a
           resolved promise instead would let a keystroke render against an empty
           list and report no match for a gene that does exist. */
        function loadGenes() {
            if (!genesPromise) {
                genesPromise = fetch(cfg.genesUrl, {credentials: 'same-origin'})
                    .then(function (r) { return r.ok ? r.json() : {genes: []}; })
                    .then(function (data) { genes = data.genes || []; })
                    .catch(function () { genes = []; });
            }
            return genesPromise;
        }

        var loadAntibodies = debounce(function (query) {
            if (!cfg.antibodiesUrl) { return; }
            fetch(cfg.antibodiesUrl + '?q=' + encodeURIComponent(query),
                  {credentials: 'same-origin'})
                .then(function (r) { return r.ok ? r.json() : {antibodies: []}; })
                .then(function (data) {
                    /* Stale answers are dropped, never painted. */
                    if (input.value.trim().toLowerCase() !== query) { return; }
                    antibodies = data.antibodies || [];
                    antibodiesFor = query;
                    render();
                })
                .catch(function () { /* leave whatever is drawn alone */ });
        }, 180);

        function position() {
            var rect = input.getBoundingClientRect();
            list.style.top = (rect.bottom + 4) + 'px';
            list.style.left = rect.left + 'px';
            list.style.width = rect.width + 'px';
        }

        function close() {
            list.style.display = 'none';
            activeIndex = -1;
        }

        function items() {
            return list.querySelectorAll('.' + itemClass);
        }

        function highlight(index) {
            Array.prototype.forEach.call(items(), function (el, i) {
                el.classList.toggle('active', i === index);
            });
        }

        /* Names the query starts, then names it appears in, then aliases. */
        function geneMatches(query) {
            var out = [];
            (genes || []).forEach(function (gene) {
                var name = (gene.name || '').toLowerCase();
                var aliases = (gene.aliases || '').toLowerCase();
                var rank = -1;
                if (name.indexOf(query) === 0) { rank = 0; }
                else if (name.indexOf(query) > -1) { rank = 1; }
                else if (aliases && aliases.indexOf(query) > -1) { rank = 2; }
                if (rank > -1) { out.push({gene: gene, rank: rank}); }
            });
            out.sort(function (a, b) {
                return a.rank - b.rank || a.gene.name.localeCompare(b.gene.name);
            });
            return out.slice(0, 6);
        }

        function row(href) {
            var el = document.createElement('div');
            el.className = itemClass;
            el.setAttribute('role', 'option');
            el.dataset.href = href;
            el.addEventListener('mousedown', function (e) {
                e.preventDefault();            // keep focus off the blur path
                window.location.href = href;
            });
            return el;
        }

        function hint(text) {
            var el = document.createElement('span');
            el.className = hintClass;
            el.textContent = text;
            return el;
        }

        function renderGene(query, match) {
            var el = row(geneUrl(match.gene.name));
            el.textContent = (match.gene.name || '').toUpperCase();
            if (match.rank === 2) {
                var alias = (match.gene.aliases || '').split(',').map(function (a) {
                    return a.trim();
                }).find(function (a) {
                    return a.toLowerCase().indexOf(query) > -1;
                });
                if (alias) { el.appendChild(hint('also: ' + alias)); }
            }
            return el;
        }

        /* Supplier and gene are the disambiguation, not decoration: a catalogue
           number is not unique across suppliers, so two hits that differ only
           there are the whole reason this is a list to pick from rather than a
           redirect. */
        function renderAntibody(ab) {
            var el = row(ab.url);
            el.textContent = ab.catalogue || ab.rrid || ab.clone;
            var parts = [];
            if (ab.supplier) { parts.push(ab.supplier); }
            if (ab.gene) { parts.push('anti-' + ab.gene); }
            if (parts.length) { el.appendChild(hint(parts.join(' · '))); }
            return el;
        }

        function render() {
            var query = input.value.toLowerCase().trim();
            list.innerHTML = '';
            activeIndex = -1;

            if (!query) { close(); return; }

            var geneHits = geneMatches(query);
            geneHits.forEach(function (match) {
                list.appendChild(renderGene(query, match));
            });

            var abHits = (antibodiesFor === query) ? antibodies : [];
            abHits.forEach(function (ab) {
                list.appendChild(renderAntibody(ab));
            });

            if (!geneHits.length && !abHits.length) {
                /* Only once the server has answered for this exact query — until
                   then the antibody half is simply not back yet, and saying "no
                   match" about a half-finished lookup is a claim we cannot make. */
                if (antibodiesFor !== query && cfg.antibodiesUrl) { close(); return; }
                var empty = document.createElement('div');
                empty.className = emptyClass;
                empty.textContent = 'No match for "' + input.value.trim() + '"';
                list.appendChild(empty);
            }

            position();
            list.style.display = 'block';
        }

        function onKeyDown(event) {
            var rows = items();
            var isOpen = list.style.display === 'block' && rows.length > 0;

            if (event.key === 'ArrowDown') {
                event.preventDefault();
                if (isOpen) { highlight(activeIndex = Math.min(activeIndex + 1, rows.length - 1)); }
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                if (isOpen) { highlight(activeIndex = Math.max(activeIndex - 1, 0)); }
            } else if (event.key === 'Enter') {
                event.preventDefault();
                if (isOpen && activeIndex >= 0) {
                    window.location.href = rows[activeIndex].dataset.href;
                } else if (isOpen && rows[0].dataset.href) {
                    window.location.href = rows[0].dataset.href;
                } else if (input.value.trim() && cfg.onEnter) {
                    cfg.onEnter(input.value.trim());
                }
                close();
            } else if (event.key === 'Escape') {
                close();
            }
        }

        input.addEventListener('focus', function () { loadGenes().then(render); });
        input.addEventListener('input', function () {
            var query = input.value.trim().toLowerCase();
            if (query) { loadAntibodies(query); }
            loadGenes().then(render);
        });
        input.addEventListener('keydown', onKeyDown);

        document.addEventListener('click', function (e) {
            if (!e.target.closest('.' + itemClass) && e.target !== input) {
                close();
            }
        });
        window.addEventListener('scroll', function () {
            if (list.style.display === 'block') { position(); }
        }, {passive: true});
        window.addEventListener('resize', function () {
            if (list.style.display === 'block') { position(); }
        });

        return {render: render, close: close};
    }

    return {create: create};
})();
