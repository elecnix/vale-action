# Parser guide

The parser reads the schema's version from the header. It stops at the first
field it cannot map and names that field in the error.

## Limits

A header longer than one kilobyte is rejected. The limit protects the reader
from a stream that never ends.
