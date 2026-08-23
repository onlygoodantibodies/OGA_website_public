/**
 * The pipeline app's stylesheet is built from this.
 *
 * Pinned to Tailwind 3 because that is what cdn.tailwindcss.com serves, so the
 * built file is the same CSS the app has been rendering all along rather than a
 * v4 rewrite of it. See bin/build_css.sh.
 *
 * `content` is the whole correctness question: Tailwind keeps a class only if it
 * finds the text of it somewhere in these files, and a class it never sees is
 * silently dropped — the element simply renders unstyled, with nothing failing.
 * So it scans more than the templates.
 *
 *   - board.js draws most of the grid markup, and the pop-outs, dialogs and
 *     banners with it.
 *   - **The Python is not optional.** pipeline/templatetags/pipeline_tags.py
 *     builds class strings from dicts — STATUS_COLORS is ten pairs of
 *     `bg-*-100 text-*-800` that appear nowhere else in the repo. Leave the .py
 *     glob out and every status badge in the app loses its colour.
 *
 * The scan is over `pipeline/**` rather than a list of the files known to emit
 * classes today, deliberately: an over-wide scan costs a few unused rules, and a
 * too-narrow one costs styling that nobody notices is gone.
 */
module.exports = {
  content: [
    './pipeline/templates/**/*.html',
    './pipeline/static/pipeline/**/*.js',
    './pipeline/**/*.py',
  ],
  theme: {
    extend: {
      // Kept identical to the inline `tailwind.config` this replaced, so the
      // navy nav stays the navy nav.
      colors: {
        ycharos: {
          50: '#f0f7ff',
          100: '#e0effe',
          200: '#b9dffc',
          300: '#7cc5fa',
          400: '#36a8f5',
          500: '#0c8de6',
          600: '#0070c4',
          700: '#01599f',
          800: '#064c83',
          900: '#0b406d',
        },
      },
    },
  },
};
