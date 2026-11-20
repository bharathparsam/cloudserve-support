"""Trusted local TF-IDF/logistic models; no network inference."""
from pathlib import Path
import hashlib,json
import numpy as np
from .contracts import Classification,Dimension
class Classifier:
    def __init__(self,model_dir):
        self.ready=False; self.metadata={}; self.models={}
        try:
            import joblib
            root=Path(model_dir)
            self.metadata=json.loads((root/'manifest.json').read_text())
            for name in ('intent','urgency'):
                p=root/f'{name}.joblib'
                if hashlib.sha256(p.read_bytes()).hexdigest()!=self.metadata['files'][p.name]: raise ValueError('Artifact fingerprint mismatch')
                self.models[name]=joblib.load(p)
            self.ready=True
        except Exception:
            self.metadata={'status':'unavailable','reason':'Trusted classifier artifacts missing or incompatible'}
    def predict(self,text):
        if not self.ready: return Classification(status='unavailable',reason=self.metadata['reason'])
        dimensions={}
        try:
            for name,bundle in self.models.items():
                raw=bundle['raw'].predict_proba([text])[0]; cal=bundle['calibrated'].predict_proba([text])[0]; classes=bundle['raw'].classes_
                order=sorted(range(len(classes)),key=lambda i:(-float(cal[i]),str(classes[i])))
                i=order[0]
                dimensions[name]=Dimension(label=str(classes[i]),raw_probabilities={str(k):float(v) for k,v in zip(classes,raw)},raw_probability=float(raw[i]),calibrated_probability=float(cal[i]),alternatives=[{'label':str(classes[j]),'raw_probability':float(raw[j]),'calibrated_probability':float(cal[j])} for j in order[1:4]])
            return Classification(status='ok',**dimensions,artifact_id=self.metadata['artifact_id'],calibration_status='measured_sigmoid')
        except Exception:
            return Classification(status='unavailable',reason='Classifier prediction failed')
