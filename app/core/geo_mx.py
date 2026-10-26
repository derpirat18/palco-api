"""Estados de México y sus ciudades principales (GEO1).

Es la semilla del catálogo de ciudades: las ciudades viven en la base
(`cities`) y PSD agrega las que falten desde el panel. Los estados son fijos
(32) y no se administran.
"""

import unicodedata

PAISES = {"MX": "México"}

ESTADOS_MX: dict[str, list[str]] = {
    "Aguascalientes": ["Aguascalientes", "Jesús María", "Calvillo"],
    "Baja California": ["Mexicali", "Tijuana", "Ensenada", "Tecate", "Rosarito"],
    "Baja California Sur": [
        "La Paz",
        "Los Cabos",
        "Cabo San Lucas",
        "San José del Cabo",
        "Loreto",
    ],
    "Campeche": ["Campeche", "Ciudad del Carmen", "Champotón"],
    "Chiapas": [
        "Tuxtla Gutiérrez",
        "San Cristóbal de las Casas",
        "Tapachula",
        "Comitán",
        "Palenque",
        "Chiapa de Corzo",
    ],
    "Chihuahua": ["Chihuahua", "Ciudad Juárez", "Delicias", "Cuauhtémoc", "Parral"],
    "Ciudad de México": ["Ciudad de México"],
    "Coahuila": [
        "Saltillo",
        "Torreón",
        "Monclova",
        "Piedras Negras",
        "Ciudad Acuña",
        "Sabinas",
    ],
    "Colima": ["Colima", "Manzanillo", "Villa de Álvarez", "Tecomán"],
    "Durango": ["Durango", "Gómez Palacio", "Lerdo"],
    "Estado de México": [
        "Toluca",
        "Ecatepec",
        "Naucalpan",
        "Nezahualcóyotl",
        "Tlalnepantla",
        "Metepec",
        "Texcoco",
        "Huixquilucan",
    ],
    "Guanajuato": [
        "Guanajuato",
        "León",
        "Irapuato",
        "Celaya",
        "Salamanca",
        "San Miguel de Allende",
    ],
    "Guerrero": ["Chilpancingo", "Acapulco", "Iguala", "Zihuatanejo", "Taxco"],
    "Hidalgo": ["Pachuca", "Tulancingo", "Tula", "Huejutla"],
    "Jalisco": [
        "Guadalajara",
        "Zapopan",
        "Tlaquepaque",
        "Tonalá",
        "Puerto Vallarta",
        "Tlajomulco",
    ],
    "Michoacán": ["Morelia", "Uruapan", "Zamora", "Lázaro Cárdenas", "Pátzcuaro"],
    "Morelos": ["Cuernavaca", "Cuautla", "Jiutepec", "Temixco"],
    "Nayarit": ["Tepic", "Bahía de Banderas", "Nuevo Vallarta"],
    "Nuevo León": [
        "Monterrey",
        "San Pedro Garza García",
        "San Nicolás de los Garza",
        "Guadalupe",
        "Apodaca",
        "Santa Catarina",
        "Linares",
    ],
    "Oaxaca": [
        "Oaxaca",
        "Juchitán",
        "Salina Cruz",
        "Tuxtepec",
        "Puerto Escondido",
        "Huatulco",
    ],
    "Puebla": [
        "Puebla",
        "Tehuacán",
        "San Andrés Cholula",
        "San Pedro Cholula",
        "Atlixco",
    ],
    "Querétaro": ["Querétaro", "San Juan del Río", "Corregidora", "El Marqués"],
    "Quintana Roo": ["Chetumal", "Cancún", "Playa del Carmen", "Cozumel", "Tulum"],
    "San Luis Potosí": [
        "San Luis Potosí",
        "Soledad de Graciano Sánchez",
        "Ciudad Valles",
        "Matehuala",
    ],
    "Sinaloa": ["Culiacán", "Mazatlán", "Los Mochis", "Guasave"],
    "Sonora": [
        "Hermosillo",
        "Ciudad Obregón",
        "Nogales",
        "San Luis Río Colorado",
        "Navojoa",
        "Guaymas",
    ],
    "Tabasco": ["Villahermosa", "Cárdenas", "Comalcalco", "Paraíso"],
    "Tamaulipas": [
        "Ciudad Victoria",
        "Reynosa",
        "Matamoros",
        "Nuevo Laredo",
        "Tampico",
        "Ciudad Madero",
        "Altamira",
    ],
    "Tlaxcala": ["Tlaxcala", "Apizaco", "Huamantla"],
    "Veracruz": [
        "Xalapa",
        "Veracruz",
        "Boca del Río",
        "Coatzacoalcos",
        "Córdoba",
        "Orizaba",
        "Poza Rica",
        "Minatitlán",
        "Tuxpan",
    ],
    "Yucatán": ["Mérida", "Valladolid", "Progreso", "Tizimín"],
    "Zacatecas": ["Zacatecas", "Fresnillo", "Guadalupe"],
}

# Como la gente escribe ciudades que ya están en el catálogo.
ALIAS = {
    "cdmx": ("Ciudad de México", "Ciudad de México"),
    "df": ("Ciudad de México", "Ciudad de México"),
    "mexico df": ("Ciudad de México", "Ciudad de México"),
    "ciudad de mexico": ("Ciudad de México", "Ciudad de México"),
    "san cristobal": ("Chiapas", "San Cristóbal de las Casas"),
    "tuxtla": ("Chiapas", "Tuxtla Gutiérrez"),
    "victoria": ("Tamaulipas", "Ciudad Victoria"),
    "juarez": ("Chihuahua", "Ciudad Juárez"),
    "obregon": ("Sonora", "Ciudad Obregón"),
}


def clave(*partes: str) -> str:
    """Sin acentos ni mayúsculas: 'Tuxtla  Gutiérrez' -> 'tuxtla gutierrez'."""
    texto = " ".join(" ".join(p.split()) for p in partes if p)
    sin = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin if not unicodedata.combining(c)).lower()


def estado_canonico(texto: str | None) -> str | None:
    """El nombre oficial del estado ('coahuila de zaragoza' -> 'Coahuila')."""
    if not texto:
        return None
    k = clave(texto)
    for estado in ESTADOS_MX:
        ke = clave(estado)
        if k == ke or k.startswith(ke + " ") or ke.startswith(k + " "):
            return estado
    if k in ("edomex", "edo mex", "mexico", "estado de mexico"):
        return "Estado de México"
    if k in ("cdmx", "df", "distrito federal"):
        return "Ciudad de México"
    return None


def ubicar(ciudad: str, estado: str | None = None) -> tuple[str, str] | None:
    """(estado, ciudad) del catálogo semilla para una ciudad escrita a mano."""
    k = clave(ciudad)
    if k in ALIAS:
        return ALIAS[k]
    candidatos = [(e, c) for e, cs in ESTADOS_MX.items() for c in cs if clave(c) == k]
    if estado:
        e = estado_canonico(estado)
        candidatos = [x for x in candidatos if x[0] == e] or candidatos
    return candidatos[0] if len(candidatos) == 1 else None
