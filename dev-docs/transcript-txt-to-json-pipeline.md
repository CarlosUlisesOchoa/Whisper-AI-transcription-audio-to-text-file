# Plan: Convertir transcripts TXT a JSON preparado para base no relacional

> Date: 2026-08-30 · Slug: `transcript-txt-to-json-pipeline` · Engram topic: `plan/transcript-txt-to-json-pipeline`
>
> Este archivo es **autocontenido**: contiene el plan completo y permite implementarlo sin
> depender de Engram. Engram existe como mecanismo adicional de recuperación; este archivo es
> el registro de respaldo.

## Context

El proyecto genera actualmente un archivo de texto por audio. Cada transcript comienza con una
cabecera fija de tres líneas y continúa con un segmento por línea:

```text
==================================================
filename:2026-08-28-refinamiento-hoja-electronica.txt
==================================================
[0.64s - 14.11s] Carlos-Ochoa: Texto transcrito...
```

Los cuatro archivos reales revisados durante la planificación contienen 248 segmentos y todos
cumplen esa estructura. Hay tres variaciones que el convertidor debe soportar:

- speakers identificados con etiquetas como `Carlos-Ochoa`;
- separadores no normalizados, por ejemplo `Felipe_Mateos`;
- speakers sin identificar, por ejemplo `unknown-06`, o segmentos sin speaker cuando la
  diarización no se ejecutó.

El objetivo posterior es consultar el conocimiento contenido en estas transcripciones desde una
base de datos no relacional. El TXT actual es útil como archivo humano y fuente de auditoría, pero
no es el formato ideal para filtrar por speaker, fecha o rango temporal, ni para construir índices
de texto o fragmentos de búsqueda.

También pueden sobrevivir artefactos menores de ASR, como palabras o frases repetidas. El
transcriber ya detecta y reintenta loops graves mediante `_looks_like_hallucination_loop` en
`transcriber.py`; este cambio agrega una segunda capa conservadora para limpiar residuos menores
durante la conversión, sin reescribir libremente el contenido.

## Goal & outcome

Implementar un convertidor independiente, determinista y basado únicamente en la biblioteca
estándar de Python que lea uno o varios transcripts TXT existentes y produzca un documento JSON
compacto, validado y versionado, listo para insertarse o transformarse para una base no relacional.

Al finalizar, el JSON debe conservar speakers, orden y timestamps útiles; agrupar segmentos
contiguos en intervenciones; limpiar repeticiones inequívocas; evitar duplicar el texto original;
y exponer una proyección opcional de chunks para sistemas de búsqueda o RAG.

## Scope

- **In scope**:
  - Parsear el formato TXT actual, tanto con speaker como sin speaker.
  - Validar cabecera, timestamps, orden temporal y líneas de segmentos.
  - Convertir timestamps decimales en segundos a enteros en milisegundos.
  - Normalizar etiquetas de speaker y asignar IDs deterministas por documento.
  - Agrupar segmentos consecutivos del mismo speaker en intervenciones (`turns`).
  - Aplicar limpieza determinista y conservadora de repeticiones claras.
  - Generar JSON UTF-8 compacto por defecto y legible mediante `--pretty`.
  - Procesar un archivo o un directorio, con modo recursivo opcional.
  - Escribir resultados de forma atómica y no sobrescribir sin autorización explícita.
  - Generar opcionalmente documentos de chunk en NDJSON, sin embeddings.
  - Agregar pruebas unitarias con `unittest` y fixtures sintéticos.
  - Documentar uso, esquema y estrategia de índices independiente del proveedor.
- **Out of scope / non-goals**:
  - Conectarse a MongoDB, DynamoDB, Cosmos DB u otro proveedor específico.
  - Crear colecciones, índices reales, credenciales o migraciones de base de datos.
  - Generar embeddings o elegir un modelo/proveedor vectorial.
  - Usar un LLM para corregir, resumir o reescribir el transcript.
  - Cambiar WhisperX, diarización, alineación, enrolamiento o calidad de transcripción.
  - Reemplazar el TXT actual o modificar el contrato de CLI, API o watcher.
  - Guardar audio, embeddings de voz o arrays de palabras alineadas dentro del JSON.

## Assumptions made

- El TXT original seguirá existiendo como única fuente de auditoría; no se duplicará como
  `raw_text` dentro de cada intervención.
- El JSON canónico conservará `start_ms` y `end_ms`. Su costo es pequeño y permiten ordenar,
  depurar y volver al punto correspondiente del audio. Las capas de búsqueda pueden excluirlos
  de su texto indexado.
- No se guardará un campo `full_text`, porque duplicaría todo el contenido de `turns[].text`.
  Cuando un consumidor lo necesite, lo derivará concatenando las intervenciones.
- El convertidor funcionará sobre transcripts históricos sin importar si Torch, WhisperX o CUDA
  están instalados.
- La salida canónica será un documento por transcript. Los chunks serán una proyección opcional y
  separada para no duplicar texto cuando no se necesite búsqueda semántica.
- No existe actualmente una infraestructura de tests en el repositorio; se usará `unittest` para
  no incorporar una dependencia nueva.

## Target canonical schema

Ejemplo ilustrativo; los valores exactos se calculan del archivo de entrada:

```json
{
  "schema_version": 1,
  "document_id": "sha256-del-txt-original",
  "source": {
    "filename": "2026-08-28-refinamiento-hoja-electronica.txt",
    "byte_size": 9330,
    "recorded_at": "2026-08-28",
    "title": "Refinamiento hoja electronica"
  },
  "language": "es",
  "duration_ms": 594910,
  "speakers": [
    {
      "id": "spk_01",
      "original_label": "Carlos-Ochoa",
      "display_name": "Carlos Ochoa",
      "identified": true
    },
    {
      "id": "spk_02",
      "original_label": "unknown-06",
      "display_name": "Unknown 06",
      "identified": false
    }
  ],
  "turns": [
    {
      "id": 1,
      "speaker_id": "spk_01",
      "start_ms": 640,
      "end_ms": 14110,
      "text": "Texto limpio de la intervención."
    }
  ],
  "processing": {
    "converter_version": 1,
    "cleanup_version": 1,
    "source_segment_count": 70,
    "turn_count": 42,
    "modified_turn_count": 3,
    "quality_flags": []
  }
}
```

Reglas del esquema:

- `schema_version` permite evolucionar el contrato sin inferencias.
- `document_id` es el SHA-256 de los bytes del TXT original. Hace la conversión idempotente:
  procesar exactamente el mismo archivo vuelve a producir el mismo ID.
- `source.recorded_at` solo se agrega cuando el nombre comienza con una fecha ISO válida.
- `source.title` se deriva del nombre después de retirar fecha y extensión; es metadata de
  conveniencia, no una corrección semántica.
- `language` es opcional y proviene de `--language`; no se intentará detectar idioma nuevamente.
- `speakers` es una lista para facilitar índices sobre `speakers.original_label` y
  `speakers.display_name` en bases documentales. Cada etiqueta original aparece una sola vez.
- `speaker_id` será `null` en turns sin diarización. No se inventará una identidad.
- `turns[].id` es un entero secuencial y estable a partir de 1.
- Campos opcionales se omiten en vez de almacenarse como `null`, salvo `speaker_id`, donde la
  ausencia de diarización es información útil.
- No se guardan `raw_text`, `full_text`, embeddings ni chunks dentro del documento canónico.
- `processing.quality_flags` y metadata por turn solo aparecen cuando contienen información.

## Optional search-chunk projection

Cuando se solicite explícitamente, el convertidor podrá generar un archivo NDJSON adicional con
un objeto por chunk. Esta proyección permite carga por streaming y evita un array gigante:

```json
{"chunk_id":"<document_id>:0001","document_id":"<document_id>","turn_ids":[1,2,3],"speaker_ids":["spk_01","spk_02"],"start_ms":640,"end_ms":88020,"text":"Texto autocontenido del chunk."}
```

- El chunk se construye por límites de turn, nunca cortando una palabra.
- Un tamaño aproximado configurable por caracteres controla el agrupamiento; no se debe depender
  de un tokenizador o modelo específico en esta fase.
- Los chunks pueden solaparse por una intervención para conservar contexto, pero el valor por
  defecto será sin solapamiento para minimizar espacio.
- No se generan chunks salvo que el usuario pase la opción correspondiente.
- Un indexador futuro puede añadir `embedding` y metadata del modelo en una colección separada.

## Approach

### 1. Crear un núcleo puro y reutilizable

Agregar `transcript_document.py`, sin imports de `transcriber.py`, WhisperX, Torch ni paquetes de
terceros. Definir tipos internos con `dataclasses` para `ParsedSegment`, `Speaker`, `Turn` y el
resultado de limpieza. Exponer funciones pequeñas y testeables:

1. `parse_transcript(text: str, source_name: str) -> ParsedTranscript`
2. `normalize_speakers(segments) -> (speakers, normalized_segments)`
3. `merge_segments_into_turns(segments, max_gap_ms) -> list[Turn]`
4. `clean_turn_text(text, cleanup_level) -> CleanupResult`
5. `build_document(parsed, source_bytes, options) -> dict`
6. `iter_search_chunks(document, max_chars, overlap_turns=0)`
7. `write_json_atomic(path, document, pretty=False)`
8. `write_ndjson_atomic(path, chunks)`

Mantener separados parseo, transformación y serialización: un error de I/O no debe mezclarse con
las reglas de dominio y las pruebas no deben necesitar archivos salvo en los casos de integración.

### 2. Parsear y validar el TXT actual

- Aceptar saltos LF y CRLF mediante `splitlines()`.
- Exigir las tres líneas de cabecera y validar `filename:<valor>`.
- Parsear segmentos con una expresión regular compilada que acepte:
  - `[0.03s - 21.44s] Speaker: texto`
  - `[0.03s - 21.44s] texto` cuando no existe speaker.
- Usar `Decimal` al interpretar segundos y convertir a milisegundos una sola vez, evitando errores
  binarios y strings temporales en el documento final.
- Rechazar timestamps negativos, `end < start` y retrocesos temporales no explicables.
- Permitir gaps y pequeños solapamientos entre speakers; las conversaciones reales pueden tenerlos.
- Para cualquier línea inválida, lanzar un error de dominio con archivo, número de línea y motivo.
- No escribir salida parcial cuando falla una línea.
- Verificar que el nombre de la cabecera sea informativo, pero usar el nombre real del archivo como
  `source.filename`. Una discrepancia se registra como quality flag, no invalida el transcript.

### 3. Normalizar speakers sin perder identidad

- Enumerar speakers por primera aparición para obtener IDs deterministas `spk_01`, `spk_02`, etc.
- Conservar `original_label` exactamente como aparece.
- Crear `display_name` reemplazando guiones y underscores por espacios y colapsando whitespace.
- No añadir acentos, corregir nombres ni fusionar etiquetas parecidas automáticamente.
- Marcar `identified: false` para patrones `unknown-NN` y `SPEAKER_NN`; el resto queda `true`.
- Cuando el segmento no tenga speaker, conservar `speaker_id: null` y no crear una entrada falsa.
- Si dos etiquetas deberían pertenecer a la misma persona, esa reconciliación queda para una capa
  posterior o un mapa explícito futuro; una heurística automática podría mezclar personas.

### 4. Agrupar segmentos en intervenciones

- Fusionar únicamente segmentos consecutivos con el mismo `speaker_id` y una separación menor o
  igual a `max_gap_ms`.
- Valor por defecto propuesto: 2,000 ms; exponerlo como opción de CLI.
- La intervención fusionada toma el inicio del primer segmento y el final del último.
- Concatenar textos con un solo espacio antes de limpiar.
- No fusionar a través de un cambio de speaker, un gap superior al umbral o ausencia/presencia
  diferente de diarización.
- Conservar en `processing.source_segment_count` el número original para observabilidad.

### 5. Limpiar repeticiones de forma conservadora

Implementar reglas deterministas y versionadas. No usar corrección generativa:

1. Normalizar whitespace y espacios alrededor de puntuación sin cambiar palabras.
2. Colapsar runs inequívocos de la misma palabra de tres o más apariciones consecutivas.
3. Detectar y colapsar frases exactas de 2 a 8 tokens repetidas tres o más veces seguidas.
4. Preservar repeticiones dobles por defecto (`no, no`, `muy muy`), porque pueden ser intención o
   énfasis. Marcarlas opcionalmente como `possible_stutter` sin modificar el texto.
5. Marcar como `suspected_repetition_loop` las intervenciones que superen umbrales de dominancia o
   edición; no borrar todo el turn.
6. Calcular la proporción de tokens eliminados. Si supera un límite configurable, conservar el
   texto original de esa intervención y añadir `cleanup_rejected_high_edit_ratio`.
7. Registrar solo códigos compactos de transformación cuando hubo cambios; no almacenar antes y
   después en el JSON. El TXT original permite auditoría.

Los valores de umbral deben vivir como constantes documentadas y cubrirse con tests. Un cambio en
las reglas incrementa `cleanup_version`.

### 6. Construir un documento determinista y compacto

- Calcular SHA-256 sobre los bytes originales antes de decodificar.
- Leer exclusivamente UTF-8; reportar un error claro si el archivo no se puede decodificar.
- Mantener orden estable de claves y listas para que el mismo input y opciones produzcan la misma
  estructura.
- No incluir hora actual dentro del documento; la fecha de ingestión corresponde a la base de
  datos y rompería la reproducibilidad del convertidor.
- Serializar por defecto con `ensure_ascii=False` y separadores compactos.
- `--pretty` usa indentación para inspección humana, sin cambiar el contenido lógico.
- Terminar el archivo con newline y escribir a un temporal en el mismo directorio antes de
  `os.replace`, siguiendo la atomicidad usada por `watcher.write_transcript`.

### 7. Agregar el CLI independiente

Crear `transcript_to_json.py` usando `argparse`, consistente con `audio_to_text_file.py`:

```powershell
$env:PYTHONUTF8=1
py transcript_to_json.py "ruta\transcript.txt"
py transcript_to_json.py "ruta\carpeta" --recursive --language es
py transcript_to_json.py "ruta\transcript.txt" --pretty --overwrite
py transcript_to_json.py "ruta\carpeta" --chunks-output "ruta\chunks.ndjson"
```

Contrato propuesto:

- Argumento posicional `input`: archivo TXT o directorio.
- `--output`: ruta de archivo para input único o directorio destino para batch.
- `--recursive`: recorrer subdirectorios; apagado por defecto.
- `--language`: metadata opcional, sin detección.
- `--merge-gap-ms`: configurar agrupación; default 2000.
- `--cleanup-level`: `off` o `conservative`; default `conservative`.
- `--pretty`: JSON indentado.
- `--overwrite`: permitir reemplazo atómico; sin la opción, los existentes se omiten.
- `--chunks-output`: NDJSON opcional. Para batch debe representar un archivo agregado o un
  directorio según se defina de forma inequívoca en `argparse`.

El resumen final del batch debe separar procesados, omitidos y fallidos, y devolver código distinto
de cero si al menos un archivo falla. No pedir confirmación interactiva: la seguridad está en no
sobrescribir salvo con `--overwrite`.

### 8. Preparar el contrato para persistencia no relacional

Mantener el convertidor independiente del proveedor y documentar estas recomendaciones:

- Índice único por `document_id` para ingestión idempotente.
- Índice por `source.recorded_at` para consultas cronológicas.
- Índices multikey/equivalentes por `speakers.original_label` y `turns.speaker_id` cuando el motor
  lo soporte.
- Índice de texto sobre `turns.text`, o colección de chunks para motores que no indexen bien arrays
  anidados.
- Colección separada `transcript_chunks` para texto vectorizable y embeddings; relacionarla con
  `document_id`.
- No guardar embeddings dentro del documento canónico: son derivados, dependen del modelo y suelen
  ocupar más espacio que el transcript.
- El proceso de ingestión debe añadir sus propios campos de auditoría (`created_at`, `updated_at`,
  versión del modelo de embedding) sin alterar el archivo JSON canónico.

### 9. Agregar pruebas y documentación

Crear `tests/test_transcript_document.py` con `unittest` y fixtures pequeños, sintéticos y sin datos
privados. Cubrir como mínimo:

- LF y CRLF.
- Header válido, filename diferente y header malformado.
- Segmentos con speaker, `unknown-06`, underscore y sin speaker.
- Unicode español y serialización sin escapes ASCII innecesarios.
- Conversión exacta de segundos decimales a milisegundos.
- Rechazo de timestamps negativos, invertidos y líneas inválidas con número de línea.
- IDs de speaker por orden de aparición y salida determinista.
- Fusión por speaker/gap y separación por cambio de speaker/gap largo.
- Limpieza de palabra repetida tres o más veces.
- Limpieza de frase repetida tres o más veces.
- Preservación de repeticiones dobles ambiguas.
- Rechazo de limpieza cuando el edit ratio es demasiado alto.
- Ausencia de `raw_text` y `full_text`.
- ID SHA-256 estable.
- Escritura atómica y protección contra overwrite.
- JSON compacto frente a `--pretty` con igualdad semántica.
- Proyección de chunks determinista y solo bajo solicitud.
- Batch con mezcla de éxitos, skips y errores.

Actualizar `README.md` con propósito, esquema resumido, comandos, decisión de almacenamiento y
ejemplo de carga conceptual, sin comprometerse con una base específica.

## Affected areas / files

- `transcript_document.py` — nuevo núcleo puro: parser, validación, speaker map, turns, limpieza,
  documento y chunks.
- `transcript_to_json.py` — nuevo CLI de archivo/directorio y escritura atómica.
- `tests/test_transcript_document.py` — nuevas pruebas unitarias con biblioteca estándar.
- `tests/fixtures/` — transcripts sintéticos mínimos para parsing e integración.
- `README.md` — uso y contrato del nuevo convertidor.
- `transcriber.py` — **sin cambios**; se reutiliza su formato documentado como contrato de entrada.
- `audio_to_text_file.py` — **sin cambios**; seguirá generando TXT como hasta ahora.
- `api.py` y `watcher.py` — **sin cambios**; integrar JSON automáticamente es una fase posterior.
- `requirements.txt` y `requirements-watcher.txt` — **sin cambios**; el convertidor es stdlib-only.

## Constraints & risks

- La limpieza puede borrar énfasis real si es agresiva. Por eso el default solo modifica patrones
  inequívocos de tres o más repeticiones y rechaza cambios demasiado grandes.
- Una etiqueta parecida no garantiza la misma identidad. No fusionar speakers por similitud textual.
- El formato sin speaker es ambiguo porque el texto también puede contener `:`. La expresión regular
  debe reconocer speaker únicamente en la posición posterior al timestamp y aceptar el resto como
  texto literal.
- Los speakers pueden solaparse temporalmente; el parser no debe asumir exclusividad.
- Un hash de contenido cambia cuando cambia cualquier byte, incluida la cabecera. Es correcto para
  idempotencia de archivo, pero una base que quiera versionar el mismo documento lógico debe usar
  `source.filename` más su propia política de versión.
- Directorios pueden contener `.txt` que no son transcripts. Deben fallar de forma explícita o
  contarse como inválidos, nunca producir JSON engañoso.
- JSON anidado y límites máximos varían entre proveedores. Un transcript de hasta dos horas cabe
  holgadamente en motores documentales comunes, pero la implementación debe medir el tamaño final y
  reportarlo; no debe asumir un límite concreto sin elegir proveedor.
- Embeddings y chunks pueden dominar el almacenamiento. Mantenerlos opcionales y separados.
- El proyecto imprime caracteres Unicode; en Windows las verificaciones deben ejecutarse con
  `PYTHONUTF8=1`.
- El repositorio tenía cambios no relacionados al redactar este plan. La implementación debe
  preservar `CLAUDE.md`, `watcher.py`, scripts y cualquier archivo ajeno a este alcance.

## Acceptance criteria / verification

- Un transcript real actual se convierte a JSON sin perder ningún speaker ni alterar el orden.
- Los 248 segmentos de las cuatro muestras revisadas son parseables con el mismo contrato.
- La suma de turns y su metadata permite rastrear el rango temporal completo del transcript.
- Todos los timestamps se guardan como enteros de milisegundos, con `start_ms <= end_ms`.
- Procesar dos veces los mismos bytes con las mismas opciones produce el mismo documento lógico y
  el mismo `document_id`.
- El JSON no contiene `raw_text`, `full_text`, embeddings ni copia del TXT completo.
- Repeticiones claras de tres o más palabras/frases se limpian; dobles ambiguos se preservan.
- Una limpieza que exceda el edit ratio no se aplica silenciosamente y deja un quality flag.
- Una línea inválida informa archivo y línea; no queda un `.json` parcial.
- Un output existente se omite sin `--overwrite` y se reemplaza atómicamente con la opción.
- La salida default es compacta y UTF-8; `--pretty` cambia solo el formato visual.
- Los chunks no se generan por defecto y, cuando se solicitan, referencian `document_id` y turns
  válidos sin cortar palabras.
- El convertidor no importa Torch, WhisperX, FastAPI ni librerías de base de datos.
- No cambia el formato TXT ni el comportamiento actual de CLI, API o watcher.
- Ejecutar pruebas:

  ```powershell
  $env:PYTHONUTF8=1
  py -m unittest discover -s tests -v
  ```

- Validar manualmente una copia de muestra, sin escribir sobre Google Drive:

  ```powershell
  $env:PYTHONUTF8=1
  py transcript_to_json.py "ruta\copia-del-transcript.txt" --pretty
  ```

- Abrir el JSON producido, cargarlo con `json.load` y verificar las invariantes del esquema en una
  prueba de integración.

## Implementation sequence and stop condition

1. Implementar primero parser, validación y schema con tests.
2. Añadir normalización de speakers y turn merging con tests.
3. Añadir cleanup conservador y quality flags con tests adversariales.
4. Añadir serialización y escritura atómica.
5. Añadir CLI de archivo y batch.
6. Añadir proyección opcional de chunks.
7. Actualizar README y ejecutar exclusivamente las verificaciones anteriores.

Detener el trabajo cuando todos los criterios de aceptación pasen. No integrar una base concreta,
no generar embeddings y no conectar automáticamente el converter al watcher/API sin una solicitud
posterior.

## Engram recovery (optional convenience)

El contexto completo también está guardado en Engram. Para recuperarlo en un chat nuevo:

```text
mem_search "plan/transcript-txt-to-json-pipeline"
mem_get_observation <id-del-resultado-superior>
```

Si Engram no está disponible, ignorar esta sección: todo lo necesario está en este archivo.

## Implementation prompt (paste into a new chat)

> Implementa el plan aprobado en `dev-docs/transcript-txt-to-json-pipeline.md`; no vuelvas a
> planificar. Si está disponible, recupera contexto adicional con
> `mem_search "plan/transcript-txt-to-json-pipeline"` y luego `mem_get_observation` sobre el primer
> resultado, pero considera el Markdown autocontenido como fuente de verdad. Sigue `AGENTS.md` y
> `CLAUDE.md`, preserva los cambios no relacionados del worktree, implementa únicamente los pasos
> de Approach y verifica todos los Acceptance criteria antes de finalizar. No conectes una base de
> datos ni generes embeddings en esta fase.
