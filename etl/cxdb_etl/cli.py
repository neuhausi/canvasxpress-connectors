"""CLI entry point: ``python -m cxdb_etl <db> [--outdir DIR] [--keep]``."""
import argparse
import sys

BUILDERS = {"gencode": "gencode", "wp": "wp", "ccle": "ccle", "tcga": "tcga"}


def main(argv=None):
    p = argparse.ArgumentParser(description="Rebuild a CanvasXpress reference DB.")
    p.add_argument("db", choices=sorted(BUILDERS), help="Which database to build.")
    p.add_argument("--outdir", default=".", help="Working/output directory.")
    p.add_argument("--keep", action="store_true", help="Keep intermediate files.")
    args = p.parse_args(argv)
    mod = __import__("cxdb_etl.%s" % BUILDERS[args.db], fromlist=["build"])
    path = mod.build(args.outdir, keep=args.keep)
    print("Built", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
