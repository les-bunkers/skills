# Transcript rules: Québec French

Applies to `stt_fixes.json` when the batch `language` is `fr` and the speakers talk Québec French. Measured on a batch of 33 podcast clips (two episodes, three transcripts per clip). The generic rules in SKILL.md still hold; these add the language-specific forms.

## Write what is said

Spoken verbatim, never standard French:
- keep « pis », « t'sais » / « tu sais », « à cause que », « y'a », « ça fait que », « fait que », « quand que »;
- keep anglicisms as said: « la job », « chiller », « second-guesser », « full », « OK »;
- elisions only when they are said: « t'es », « j'suis », « y'a ». Written « tu es » for a spoken « t'es » is a fix to make; « j'suis » for a clear « je suis » is not;
- « l'IA » for artificial intelligence, never « l'AI » unless the speaker says « un AI ».

## Which source to trust

- Words STT (the reference; Grok in the measured batch) mishears rare words and returns a common word with the same sounds: « l'air » = l'IA, « la tâche » = la tarte, « qu'il est richement » = qu'il génère, « s'accruer » = s'accroître, « une proportionnée » = disproportionné, « restaurant vidéo » = réalisation vidéo. The topic usually settles it.
- YouTube auto-captions drop fillers and short function words (« te », « la ») and write numbers as digits (« de 1 » = d'un).
- OpenAI smooths « euh » and restarts and sometimes paraphrases a whole phrase: good for content words, weak on exact wording. A short clip's OpenAI text can also run off-topic (one cached result described a different moment): ignore it then and decide from the other two.
- A speaker's habits help: a speaker who says « quand que » elsewhere probably said it here. Still mark it `CHECK:` when the audio can't tell.

Example entries:

```json
{"hunk": 8, "text": "la réalisation", "why": "known STT mishearing (« restaurant vidéo »; captions « la réalisation »)"},
{"hunk": 1, "text": "qu'on", "why": "CHECK: captions « quand qu'on pense », reference « quand on »; one stop + vowel between « quand » and « pense » (/k/ or liaison /t/, can't tell); this speaker says « quand que » in other clips"}
```

## Automatic handling (vc_lang.py, fr)

- Ignored when comparing: « euh », « heu », « hum », « bah », « ben », « hein », « mm », « hmm ».
- Dropped from captions: « euh », « heu », « hum », « bah », « mm », « hmm ». « ben » and « hein » stay: « ben oui », « c'est fou, hein » carry meaning.
- Exact doubles kept: très, vraiment, beaucoup, longtemps, full, qui (« c'est qui qui »). Three or more collapse.
- A caption line never ends on: le la les l un une des du de d au aux à a en et ou pis puis mais que qu qui ce ça c se s ne n je j tu t il elle on nous vous ils elles me m te mon ma mes ton ta tes son sa ses notre votre leur leurs pour par sur dans avec sans chez très si y est va vais cette cet ces parce jusqu quand lorsque puisque dont comme fait. « plus » is not on the list: it ends phrases (« qu'il contribue plus »).

## Prompt for the OpenAI pass

Write the prompt in French; it steers spelling and register. Config example:

```json
"transcription": {"prompt": "Balado en français québécois. {people}. Sujet : {topic} Vocabulaire : {vocabulary}.",
                  "vocabulary": ["l'IA", "intelligence artificielle", "job", "business"]}
```

`people` comes from the episode (« l'invité et l'animatrice »), `topic` from the clip.
