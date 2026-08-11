Your previous SQL was rejected by the validator before it ran:
{{error}}

Previous SQL:
{{sql}}

Fix it -- almost certainly it wasn't a single, schema-qualified SELECT statement. Address this specific rejection, don't just rewrite from scratch.
