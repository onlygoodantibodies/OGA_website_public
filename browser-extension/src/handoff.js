/**
 * The hand-off to the OGA MCP connector — one URL and one claim, shared.
 *
 * Two surfaces point at the connector and they must not drift: the popup, which
 * hands over a whole paper, and the hover card, which hands over one antibody's
 * unanswered question. What has to stay identical is the destination and what we
 * claim the connector is better at; the sentence around it is each surface's own.
 *
 * Why there is a hand-off at all. Both of the extension's weakest jobs are jobs
 * the connector does properly, because it reads the whole paper rather than the
 * characters near a catalogue number:
 *
 *   - CONTROLS. Removed from the extension in 0.1.7 on the measurement: page-level
 *     cue detection reached specificity 0.398 and kappa 0.114, and reported a
 *     selectivity control on 64% of papers where the true rate was 13%. A regex
 *     cannot tell which antibody a knockout belongs to. The same pipeline through
 *     a model reached kappa 0.508.
 *   - APPLICATION. Demoted to a hint in 0.2.0 on the re-run: named for 39 of 76
 *     western-blot papers, 3 of 16 immunofluorescence, and 0 of 25 IHC.
 *
 * So this is not a cross-sell. It is the honest destination for the two questions
 * the extension has measured itself unable to answer, and it must keep saying so
 * without implying the extension has answered them.
 */
(function (root) {
  "use strict";

  root.OGAHandoff = {
    URL: "https://onlygoodantibodies.co.uk/tools/connect-your-ai/",
    LABEL: "Check this paper with your AI",
    // Present tense, about the connector — never a hedge about the verdict above
    // it, which is a hash lookup and is not in doubt.
    WHY: "Which application a paper used, and whether it controlled its "
       + "antibodies, are read from the whole paper by the OGA connector.",
    // The same offer with the application half taken out, for the card that has
    // already made it: when a reading is doubted the caution carries the link
    // itself, and repeating the whole sentence under the chips puts two routes to
    // one page three lines apart. The controls half is still worth saying — it is
    // a question the reader has not been prompted to ask.
    WHY_CONTROLS: "Whether a paper controlled its antibodies is also read from "
                + "the whole paper by the OGA connector.",
  };
})(typeof globalThis !== "undefined" ? globalThis : window);
