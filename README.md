# Door Number Detector 🚪

Validação de portas através do Google Street View com OCR (Tesseract)

## Descrição

Aplicação Python que detecta automaticamente números de portas em coordenadas geográficas usando:
- **Google Street View API** para obter imagens das ruas
- **Tesseract OCR** para extrair números das imagens
- **Suporte a múltiplos bancos de dados** (Oracle, PostgreSQL, MySQL, SQLite)

### Coordenadas de Exemplo
- **Latitude:** 40.80082
- **Longitude:** -8.593741

## Requisitos

### Sistema
- Python 3.8+
- Tesseract OCR instalado

### Instalação do Tesseract

**Ubuntu/Debian:**
```bash
sudo apt-get install tesseract-ocr
```

**macOS:**
```bash
brew install tesseract
```

**Windows:**
- Download do installer: https://github.com/UB-Mannheim/tesseract/wiki

### Dependências Python
```bash
pip install -r requirements.txt
```

## Configuração

### 1. Google API Key
1. Acesse [Google Cloud Console](https://console.cloud.google.com/)
2. Crie um novo projeto
3. Ative as APIs:
   - Street View Static API
   - Vision API
4. Crie uma chave de API
5. Defina a variável de ambiente ou em `config.ini`:

```bash
export GOOGLE_API_KEY="sua_chave_aqui"
```

### 2. Banco de Dados

#### Oracle
```ini
[DATABASE]
type = oracle
host = localhost
port = 1521
user = system
password = sua_senha
sid = xe
```

Instalar cx_Oracle:
```bash
pip install cx-Oracle
```

#### PostgreSQL
```ini
[DATABASE]
type = postgresql
host = localhost
port = 5432
user = postgres
password = sua_senha
database = door_detector
```

#### MySQL
```ini
[DATABASE]
type = mysql
host = localhost
port = 3306
user = root
password = sua_senha
database = door_detector
```

#### SQLite (padrão)
```ini
[DATABASE]
type = sqlite
```

### 3. Schema do Banco de Dados

A tabela é criada automaticamente na primeira execução:

```sql
CREATE TABLE door_detection_results (
    id NUMBER PRIMARY KEY,
    latitude FLOAT NOT NULL,
    longitude FLOAT NOT NULL,
    door_number VARCHAR2(50),
    confidence FLOAT,
    heading NUMBER,
    pitch NUMBER,
    timestamp DATE DEFAULT SYSDATE,
    raw_ocr_text CLOB,
    status VARCHAR2(20) DEFAULT 'success',
    error_message CLOB
);
```

## Uso

### Execução Básica
```bash
python main.py
```

### Processamento em Lote
```python
from main import DoorNumberDetector

detector = DoorNumberDetector()

coordinates = [
    {'latitude': 40.80082, 'longitude': -8.593741},
    {'latitude': 40.80100, 'longitude': -8.59400},
    {'latitude': 40.80150, 'longitude': -8.59500}
]

results = detector.process_coordinates_batch(coordinates)

for result in results:
    print(f"{result['latitude']}, {result['longitude']}: {result['door_number']}")

detector.close()
```

### Consultar Resultados Armazenados
```python
from main import DoorNumberDetector

detector = DoorNumberDetector()
results = detector.db.get_results(limit=50)

for result in results:
    print(result)

detector.close()
```

## Estrutura do Projeto

```
door_number_detector/
├── main.py                    # Script principal
├── config.py                  # Gerenciamento de configurações
├── database.py                # ORM e operações de BD
├── google_street_view.py      # Integração com Street View
├── requirements.txt           # Dependências
├── config.ini                 # Arquivo de configuração
├── door_detector.log          # Logs (gerado na execução)
└── README.md                  # Este arquivo
```

## Logs

Os logs são salvos em `door_detector.log`:

```
2024-03-06 14:30:45,123 - INFO - Door Number Detector inicializado
2024-03-06 14:30:46,456 - INFO - Processando coordenada: 40.80082, -8.593741
2024-03-06 14:30:48,789 - INFO - Resultado: 123 (confiança: 85%)
```

## Troubleshooting

### "Tesseract is not installed"
```bash
# Ubuntu
sudo apt-get install tesseract-ocr

# Ou configure o caminho em config.ini:
[OCR]
tesseract_path = /usr/bin/tesseract
```

### "Could not connect to database"
- Verifique credenciais em `config.ini`
- Verifique se o servidor de BD está rodando
- Confirme a porta correta

### "Invalid API Key"
- Verifique a chave no Google Cloud Console
- Confirm que Street View API está habilitada
- Reinicie a aplicação

## Resultado da Detecção

```json
{
    "success": true,
    "latitude": 40.80082,
    "longitude": -8.593741,
    "door_number": "123",
    "confidence": 85,
    "heading": 0,
    "pitch": 0,
    "timestamp": "2024-03-06T14:30:47.123456"
}
```

## Performance

- **Tempo por coordenada:** ~2-3 segundos
- **Confiança OCR:** 70-95% (depende da qualidade da imagem)
- **Suporta:** Processamento em lote de múltiplas coordenadas

## Licença

MIT License

## Contribuições

Contribuições são bem-vindas! Por favor, abra uma issue ou pull request.