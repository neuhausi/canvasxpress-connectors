# cxdb_etl — rebuild the CanvasXpress reference databases

Python port of the legacy Perl `*Services.pl` **update** pipelines that build the
read-only SQLite databases served through `canvasxpress-connectors` (see the
"Packed-matrix sources" section of the top-level README).

```
python -m cxdb_etl gencode --outdir /tmp/build     # → /tmp/build/gencode.sqlite
python -m cxdb_etl wp      --outdir /tmp/build      # → /tmp/build/wp.sqlite
```

Each builder downloads its public sources, reshapes them, and assembles the SQLite
file with the exact schema the serving layer expects. Move the result to your data
dir (e.g. `/home/canvasxpress/data/sqlite/`) and register a connector source.

## Databases

| module | database | sources |
| --- | --- | --- |
| `gencode` | `gencode.sqlite` | GENCODE GTF + EBI GWAS catalog |
| `wp` | `wp.sqlite` | WikiPathways GMT/GPML + NCBI gene_info/taxonomy |
| `ccle` | `ccle.sqlite` | CCLE (Figshare) expression/CNV/mutations + GENCODE probemap |

**Status**
- `gencode`, `wp` — full download→build pipelines, tested on synthetic inputs.
- `ccle` — the **packed-matrix reshape** (`pack_matrix`), the `json`/`indices`
  template builders, and `assemble()` are complete and tested (a mini build is read
  back through the Phase-1 `PackedMatrixSource`). The `build()` download wrapper is a
  stub: the CCLE sample-info / mutation **column layouts drift between Figshare
  releases**, so wire the current URLs + confirm those column indices before a real
  run — the reshape core is format-independent.
- `refseq` — **not ported**: its build script (genome/hapmap/gwas) was not among the
  provided Perl sources; `genomeServices.pl` is the *serve* side only.
- `tcga` — deferred (the Perl `update` is largely commented out and the inputs are
  large Xena dumps).

## The packed format

CCLE expression/CNV are stored **packed**: one row per gene = a JSON array of that
gene's values across a *sorted* sample list, plus a shared `json` template holding the
sample axis + annotations once, and an `indices` table pairing samples across data
types (for correlation). `pack_matrix` writes it; `PackedMatrixSource` reads it.

## Tests

```
pip install pytest && python -m pytest etl/tests
```
