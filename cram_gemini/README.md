# Gemini Practice Generation

This folder is for generating brand-new SAT practice sets from the diagnostic outputs of the scraper.

The intended flow is:

1. Start from [`wrong_questions.llm.md`](/Users/pavel/dev/sat/cram_gemini/wrong_questions.llm.md), which contains the scraped misses in an LLM-friendly format.
2. Use that to produce or refine [`weakness_blueprint.md`](/Users/pavel/dev/sat/cram_gemini/weakness_blueprint.md), which summarizes the recurring mistakes, traps, and priority topics.
3. Run Gemini against [`practice_gen.md`](/Users/pavel/dev/sat/cram_gemini/practice_gen.md) to generate a fresh practice set that matches those weaknesses.

`practice_gen.md` is written so Gemini generates:

- one questions file: `sat_practice_gemini_<YYMMDD>_q.md`
- one answers/explanations file: `sat_practice_gemini_<YYMMDD>_a.md`

That means this folder can be reused whenever the diagnostic files change: update the underlying `wrong_questions.llm.md`, refresh the blueprint, and generate a new custom practice set targeted at the current weak areas.

Run:

```bash
gemini --model gemini-3-pro-preview -p "execute @practice_gen.md"
```
