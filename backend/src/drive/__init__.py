"""Google Drive source: gateway (real and fake), link, category and reason rules.

Import-light on purpose: nothing here imports Google libraries. The real
gateway lives in ``drive.real`` and is loaded only by
``drive.select.build_drive_gateway`` when Drive is really used.
``drive.convert`` belongs to another module and must stay importable
without this file.
"""
