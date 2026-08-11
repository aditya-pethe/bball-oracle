Your previous SQL ran without error but returned zero rows:

Previous SQL:
{{sql}}

That is almost always a filter, join key, or literal-matching problem (e.g. a name spelled differently than it's stored, an overly strict WHERE, a bad join condition) -- find and fix the specific cause rather than redrafting from scratch.
