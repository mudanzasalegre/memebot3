"""
ml package
~~~~~~~~~~
Funciones de entrenamiento y modelos para MemeBot 3.

• `train.train_and_save()`   – entrenamiento completo + persistencia  
• `retrain.retrain_if_better()` – lógica de re-entrenos semanales
"""
from .train import train_and_save          # noqa: F401
