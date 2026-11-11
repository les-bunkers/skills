"""Language tables for the vertical-clips scripts: fillers, weak caption-line endings, doubles kept on purpose.

Imported by stt_reconcile.py and build_vertical.py. Stdlib only, no CLI. French ("fr") is the measured set (a batch of
Québec French podcast clips); English ("en") is a starting point, not yet run on real material. Another language =
add its three sets here; an unknown language falls back to the Words `type: filler` field only.
"""

# Ignored when comparing transcripts (one STT keeps « euh », another drops it).
COMPARE_IGNORE = {
    "fr": {"euh", "heu", "hum", "bah", "ben", "hein", "mm", "hmm"},
    "en": {"um", "uh", "uhm", "erm", "er", "hmm", "mm", "mhm"},
}

# Dropped from the burned-in captions. French keeps « ben » and « hein »: they carry meaning in spoken Québec French
# (« ben oui », « c'est fou, hein »).
CAPTION_DROP = {
    "fr": {"euh", "heu", "hum", "bah", "mm", "hmm"},
    "en": {"um", "uh", "uhm", "erm", "er", "hmm", "mm", "mhm"},
}

# A caption group never ends on one of these (articles, prepositions, subject pronouns, conjunctions): length-only
# grouping produced « jobs le fun en | … » and « prendre les jobs le | … ». French "plus" is NOT weak: it ends phrases
# too (« qu'il contribue plus »).
WEAK = {
    "fr": set("""le la les l un une des du de d au aux à a en et ou pis puis mais que qu qui ce ça c se s ne n je j tu t
il elle on nous vous ils elles me m te mon ma mes ton ta tes son sa ses notre votre leur leurs pour par sur dans
avec sans chez très si y est va vais cette cet ces parce jusqu quand lorsque puisque dont comme fait""".split()),
    "en": set("""a an the of to in on at for with from by and or but nor so that which who whom whose this these those
i you he she we they it my your his her our their its me him us them is are was were be been am do does did if
because when as than then into onto about like just very""".split()),
}

# Exactly two in a row are kept (emphasis, Québec « c'est qui qui »); three or more still collapse as a stutter.
KEEP_DOUBLE = {
    "fr": {"très", "vraiment", "beaucoup", "longtemps", "full", "qui"},
    "en": {"very", "really", "so", "no", "yes"},
}


def table(name, language):
    """One of the sets above for `language` ("fr-CA" -> "fr"); empty set when the language has none."""
    return globals()[name].get((language or "").split("-")[0].lower(), set())
