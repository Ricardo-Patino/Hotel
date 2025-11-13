from extensions import db
from sqlalchemy import Integer, String, Date, DateTime, Numeric, Boolean, Text

# ------------------------------------------------------------------
# Modelo BASE: Funcionario (tabla ya existente en la BD)
# ------------------------------------------------------------------
class Funcionario(db.Model):
    __tablename__ = 'Funcionario'

    Codigo_Funcionario   = db.Column(Integer, primary_key=True)
    Cedula               = db.Column(String(20), nullable=False, index=True)
    Nombre               = db.Column(String(50), nullable=False)
    Apellido             = db.Column(String(50), nullable=False)
    Puesto               = db.Column(String(50), nullable=False)
    Fecha_Nacimiento     = db.Column(Date, nullable=False)

    # Extensiones HRM-08-001
    Departamento         = db.Column(String(80))
    Fecha_Ingreso        = db.Column(Date)                 # En BD: NOT NULL con default CURDATE(); aquí lo dejamos aceptando null (lo pone la BD)
    Salario_Base_Mensual = db.Column(Numeric(12, 2), default=0)
    Estado_Empleado      = db.Column(String(20), default='Activo')  # Enum en BD
    Tipo_Contrato        = db.Column(String(30), default='Tiempo completo')  # Enum en BD
    Cuenta_Bancaria      = db.Column(String(60))
    Banco                = db.Column(String(60))

    # Tiempos de auditoría
    Fecha_modificacion   = db.Column(DateTime)             # manejado por BD
    Fecha_mod_alta       = db.Column(DateTime)             # manejado por BD

    def __repr__(self):
        return f"<Funcionario {self.Codigo_Funcionario} {self.Nombre} {self.Apellido}>"

# ------------------------------------------------------------------
# RRHH: Marcación (usado como 'HoraExtra' en rutas existentes)
# ------------------------------------------------------------------
class Marcacion(db.Model):
    __tablename__ = 'Marcacion'

    Id                 = db.Column(Integer, primary_key=True)
    Codigo_Funcionario = db.Column(Integer, db.ForeignKey('Funcionario.Codigo_Funcionario'), nullable=False, index=True)
    Fecha              = db.Column(Date, nullable=False, index=True)
    Hora_Entrada       = db.Column(DateTime)
    Hora_Salida        = db.Column(DateTime)
    Horas_Regulares    = db.Column(Numeric(6, 2))
    Estado             = db.Column(String(12), nullable=False, default='Abierta', index=True)  # Enum en BD
    Observaciones      = db.Column(String(255))
    Fecha_Creacion     = db.Column(DateTime)
    Fecha_Actualiza    = db.Column(DateTime)

    funcionario = db.relationship('Funcionario', backref='marcaciones', lazy='joined')

    def __repr__(self):
        return f"<Marcacion Id={self.Id} Func={self.Codigo_Funcionario} Fecha={self.Fecha}>"

# Alias de compatibilidad para blueprints que importan 'HoraExtra'
HoraExtra = Marcacion

# ------------------------------------------------------------------
# RRHH: Cuentas bancarias
# ------------------------------------------------------------------
class HRMBankAccount(db.Model):
    __tablename__ = 'hrm_bankaccount'

    Id_BankAcc          = db.Column(Integer, primary_key=True)
    Codigo_Funcionario  = db.Column(Integer, db.ForeignKey('Funcionario.Codigo_Funcionario'), nullable=False, index=True)
    Banco               = db.Column(String(80), nullable=False)
    Tipo_Cuenta         = db.Column(String(20), nullable=False, default='Ahorros')  # Enum en BD
    Numero_Cuenta       = db.Column(String(40), nullable=False, unique=True)
    Moneda              = db.Column(String(5), nullable=False, default='CRC')       # Enum en BD
    Activa              = db.Column(Boolean, nullable=False, default=True)
    Fecha_Creacion      = db.Column(DateTime)

    funcionario = db.relationship('Funcionario', backref='cuentas_bancarias', lazy='joined')

    def __repr__(self):
        return f"<HRMBankAccount {self.Numero_Cuenta} Func={self.Codigo_Funcionario}>"

# ------------------------------------------------------------------
# RRHH: Deducciones voluntarias
# ------------------------------------------------------------------
class HRMVoluntaryDed(db.Model):
    __tablename__ = 'hrm_voluntaryded'

    Id_VolDed           = db.Column(Integer, primary_key=True)
    Codigo_Funcionario  = db.Column(Integer, db.ForeignKey('Funcionario.Codigo_Funcionario'), nullable=False, index=True)
    Nombre              = db.Column(String(120), nullable=False)
    Monto               = db.Column(Numeric(12, 2), nullable=False, default=0)
    Periodicidad        = db.Column(String(20), nullable=False, default='Mensual')   # Enum en BD
    Activa              = db.Column(Boolean, nullable=False, default=True)
    Fecha_Creacion      = db.Column(DateTime)

    __table_args__ = (
        db.UniqueConstraint('Codigo_Funcionario', 'Nombre', name='UQ_hrm_volded'),
    )

    funcionario = db.relationship('Funcionario', backref='deducciones_voluntarias', lazy='joined')

    def __repr__(self):
        return f"<HRMVoluntaryDed {self.Nombre} Func={self.Codigo_Funcionario} Monto={self.Monto}>"

# ------------------------------------------------------------------
# RRHH: Historial de funcionario
# ------------------------------------------------------------------
class FuncionarioHistorial(db.Model):
    __tablename__ = 'FuncionarioHistorial'

    Id_Historial        = db.Column(Integer, primary_key=True)
    Codigo_Funcionario  = db.Column(Integer, db.ForeignKey('Funcionario.Codigo_Funcionario'), nullable=False, index=True)
    Fecha_Evento        = db.Column(DateTime, nullable=False)
    Tipo_Evento         = db.Column(String(30), nullable=False)  # Enum en BD
    Detalle             = db.Column(String(500))
    Valor_Anterior      = db.Column(String(200))
    Valor_Nuevo         = db.Column(String(200))
    Registrado_Por      = db.Column(Integer)

    funcionario = db.relationship('Funcionario', backref='historial', lazy='joined')

    def __repr__(self):
        return f"<FuncHist Func={self.Codigo_Funcionario} Tipo={self.Tipo_Evento} Fecha={self.Fecha_Evento}>"
