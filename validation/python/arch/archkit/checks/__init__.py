"""The bundled checks. `archkit run` collects this package with `--pyargs archkit.checks`.

Each module reads its own section of the adopter's configuration; `archkit run` refuses a
configuration that leaves a check's section out, so every check always runs."""
