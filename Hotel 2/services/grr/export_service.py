# services/grr/export_service.py
import io
import pandas as pd

class ExportService:
    def to_excel(self, reservas_rows: list[dict]) -> bytes:
        df = pd.DataFrame(reservas_rows)
        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Reservas')
        return bio.getvalue()
