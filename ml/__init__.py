"""
ml package
~~~~~~~~~~
Pipeline de entrenamiento, calibración de threshold y retraining.

Se evita importar submódulos pesados en import-time para no contaminar
`python -m ml.train` / `python -m ml.retrain` con warnings de runpy.
"""

__all__: list[str] = []
