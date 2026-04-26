# UI Visual Charter

## Direccion

La UI debe sentirse como una `industrial editorial operations desk`.

Eso implica:

- sobria
- tecnica
- densa pero legible
- con peso visual
- sin parecer exchange retail
- sin parecer plantilla admin generica

## No es esto

Queda explicitamente prohibido:

- grid infinita de metric cards iguales
- glassmorphism
- glow decorativo
- charts por relleno
- gradientes de texto
- look AI cyan/purple
- negro puro sobre blanco puro
- cards dentro de cards

## Tema y atmosfera

La direccion base es `mineral editorial`.

- neutrales frios con tinte petroleo y oliva
- acento controlado, no neon
- contraste alto para datos
- profundidad por capas y materiales, no por blur

## Tokens base

Usar variables CSS desde el principio.

### Light theme

```css
:root {
  --bg-canvas: oklch(0.96 0.01 95);
  --bg-surface: oklch(0.92 0.01 95);
  --bg-elevated: oklch(0.88 0.015 95);
  --border-subtle: oklch(0.78 0.02 105);
  --text-strong: oklch(0.22 0.02 110);
  --text-muted: oklch(0.46 0.02 110);
  --accent-primary: oklch(0.52 0.10 180);
  --accent-success: oklch(0.62 0.10 150);
  --accent-warn: oklch(0.72 0.12 80);
  --accent-danger: oklch(0.58 0.15 30);
  --accent-info: oklch(0.60 0.08 240);
}
```

### Dark theme

```css
[data-theme="dark"] {
  --bg-canvas: oklch(0.20 0.015 235);
  --bg-surface: oklch(0.24 0.018 235);
  --bg-elevated: oklch(0.29 0.02 235);
  --border-subtle: oklch(0.36 0.02 235);
  --text-strong: oklch(0.92 0.01 95);
  --text-muted: oklch(0.74 0.02 95);
  --accent-primary: oklch(0.72 0.09 180);
  --accent-success: oklch(0.76 0.10 150);
  --accent-warn: oklch(0.78 0.11 80);
  --accent-danger: oklch(0.70 0.14 30);
  --accent-info: oklch(0.74 0.08 240);
}
```

Regla:

- light y dark deben nacer del mismo sistema
- ninguna de las dos puede ser una ocurrencia tardia

## Tipografia

### Familias

- display y navegacion: `Sora`
- cuerpo y labels: `Source Sans 3`
- datos, ticks y tablas densas: `IBM Plex Mono`

### Reglas

- body minimo `16px`
- usar `clamp()` para escalas fluidas
- numeros con `tabular-nums`
- encabezados compactos y con tension
- evitar pesos muy finos

### Escala sugerida

```css
--fs-hero: clamp(2rem, 1.3rem + 2vw, 3.5rem);
--fs-h1: clamp(1.5rem, 1.2rem + 1vw, 2.25rem);
--fs-h2: clamp(1.125rem, 1rem + 0.5vw, 1.5rem);
--fs-body: 1rem;
--fs-small: 0.875rem;
--fs-mono: 0.875rem;
```

## Layout

### Shell

- sidebar `264px` desktop
- topbar `56px`
- content con gutters amplios
- panel principal y panel contextual asimetricos

### Composicion

- no envolver todo en cards
- usar superficies solo donde ayuden a segmentar
- tablas, timelines y strips de estado son componentes nobles
- Overview debe tener un hero operativo, no una fila de KPIs clones

### Spacing

- base `4px`
- espaciado semantico, no numerologia aleatoria
- densidad alta en tablas, aire suficiente en encabezados y bloques de estado

## Componentes nobles

Los componentes visuales que deben marcar la identidad:

- `status hero`
- `regime health strip`
- `timeline rail`
- `data table`
- `command panel`
- `source health strip`

Los charts son secundarios. Se usan para responder preguntas concretas.

## Datos y visualizacion

### Tablas

- primera clase, no fallback
- cabeceras fijas
- densidad seleccionable
- columnas numericas alineadas a la derecha
- columnas de identificador en mono

### Timelines

- Discovery y Replay deben usar timeline, no solo tabla plana
- cada evento con tipo, severidad, timestamp y resumen corto

### Charts

- `ECharts`
- sin donuts decorativos
- preferir lineas, barras horizontales e histogramas
- maximo una visual primaria por viewport

## Motion

Usar motion con utilidad operacional.

### Tokens

- `100ms`: hover y focus
- `300ms`: drawers, tabs, filtros
- `500ms`: transiciones de layout y carga inicial

### Reglas

- solo `transform` y `opacity`
- respetar `prefers-reduced-motion`
- nada de bounce
- nada de elasticidad juguetona

## Interaccion

### Estados obligatorios

Todo componente interactivo debe contemplar:

- default
- hover
- focus-visible
- active
- disabled
- loading
- success
- error

### Reglas

- focus ring visible y consistente
- skeletons en vez de spinners largos
- confirmacion explicita en acciones mutantes
- `undo` cuando sea trivial y seguro
- atajos de teclado en tablas y timeline cuando aporte

## Copy y tono

La UI escribe como una consola de operaciones, no como marketing.

### Reglas

- labels en formato `verbo + objeto` o `sustantivo claro`
- timestamps absolutos visibles
- errores concretos y accionables
- nada de bromas en mensajes de error
- misma terminologia que en runtime y API

Ejemplos correctos:

- `Pause discovery`
- `Buys paused`
- `Runtime state stale`
- `Research scorecard older than candidate feed`

## Calidad visual minima

Antes de aceptar una pantalla, debe pasar estos tests:

### Squint test

Al entrecerrar los ojos se entienden:

- jerarquia
- estado
- accion principal

### Density test

La pantalla soporta mucha informacion sin volverse barro.

### Identity test

Si tapas el logo, no parece un admin template ni un exchange retail.

### Truthfulness test

Si un bloque esta `stale` o `degraded`, visualmente se nota.

## Consecuencia para `PR-UI-8`

El design system inicial debe incluir:

- tokens de color y tipografia
- tokens de spacing y motion
- tablas densas
- chips de estado
- timeline rail
- drawer system
- banners de `stale` y `degraded`
