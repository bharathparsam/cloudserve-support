"""Development-only fitting. Validation files are never opened."""
import argparse,hashlib,json,platform,sys
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
import joblib,sklearn
from sklearn.model_selection import train_test_split,StratifiedKFold,cross_val_score
from sklearn.pipeline import Pipeline,FeatureUnion
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import classification_report,confusion_matrix,accuracy_score
from src.ingest import sanitize_text

def hash_bytes(x): return hashlib.sha256(x).hexdigest()
def text(t): return sanitize_text(t.get('subject','')+'\n'+t['body'],[t.get('customer_name',''),t.get('customer_id','')])
def bins(y,p,classes):
    best=p.argmax(axis=1); conf=p.max(axis=1); correct=np.asarray(classes)[best]==y
    return [{'lower':i/10,'upper':(i+1)/10,'count':int(m.sum()),'mean_confidence':float(conf[m].mean()) if m.any() else None,'accuracy':float(correct[m].mean()) if m.any() else None} for i in range(10) for m in [(conf>=i/10)&((conf<(i+1)/10) if i<9 else (conf<=1))]]
def run(args):
    p=Path(args.input)
    if 'validation' in p.name.lower(): raise ValueError('Training accepts development data only')
    raw=p.read_bytes(); data=json.loads(raw); root=Path(args.output); root.mkdir(parents=True,exist_ok=True)
    ids=np.arange(len(data)); labels=[x['labels']['intent'] for x in data]
    fitting,check=train_test_split(ids,test_size=.2,random_state=42,stratify=labels)
    train,cal=train_test_split(fitting,test_size=.25,random_state=42,stratify=[labels[i] for i in fitting])
    split={'seed':42,'data_hash':hash_bytes(raw),'train':train.tolist(),'calibration':cal.tolist(),'check':check.tolist(),'id_hashes':{str(i):hash_bytes(str(data[i].get('ticket_id',i)).encode()) for i in ids}}
    (root/'split-manifest.json').write_text(json.dumps(split,indent=2)) # BEFORE any fitting
    texts=np.array([text(t) for t in data]); report={'split_counts':{k:len(v) for k,v in [('train',train),('calibration',cal),('check',check)]},'dimensions':{}}
    files={}; classifiers={}
    for dimension in ('intent','urgency'):
        y=np.array([t['labels'][dimension] for t in data]); candidates=[]
        for kind in ('word','word_char'):
            feats=[('word',TfidfVectorizer(ngram_range=(1,2),sublinear_tf=True))]
            if kind=='word_char': feats.append(('char',TfidfVectorizer(analyzer='char_wb',ngram_range=(3,5),min_df=2,sublinear_tf=True)))
            pipe=Pipeline([('features',FeatureUnion(feats)),('model',LogisticRegression(C=4,max_iter=1500,random_state=42))])
            score=float(cross_val_score(pipe,texts[train],y[train],cv=StratifiedKFold(3,shuffle=True,random_state=42),scoring='f1_macro').mean()); candidates.append((score,kind,pipe))
        score,kind,model=max(candidates,key=lambda x:(x[0],x[1])); model.fit(texts[train],y[train])
        calibrated=CalibratedClassifierCV(FrozenEstimator(model),method='sigmoid'); calibrated.fit(texts[cal],y[cal]); classifiers[dimension]=(model,calibrated)
        rp=model.predict_proba(texts[check]); cp=calibrated.predict_proba(texts[check]); pred=model.classes_[cp.argmax(axis=1)]
        report['dimensions'][dimension]={'candidates':[{'features':k,'train_cv_macro_f1':s} for s,k,_ in candidates],'selected':kind,'calibrated_check':classification_report(y[check],pred,output_dict=True,zero_division=0),'raw_check':classification_report(y[check],model.predict(texts[check]),output_dict=True,zero_division=0),'classes':model.classes_.tolist(),'confusion_matrix':confusion_matrix(y[check],pred,labels=model.classes_).tolist(),'raw_reliability':bins(y[check],rp,model.classes_),'calibrated_reliability':bins(y[check],cp,model.classes_)}
        joblib.dump({'raw':model,'calibrated':calibrated},root/f'{dimension}.joblib'); files[f'{dimension}.joblib']=hash_bytes((root/f'{dimension}.joblib').read_bytes())
    model,calibrated=classifiers['intent']; probs=calibrated.predict_proba(texts[cal]); pred=model.classes_[probs.argmax(axis=1)]; confidence=probs.max(axis=1); correct=pred==np.array(labels)[cal]
    # Measured global floor: highest coverage among floors with >=10 examples and >=90% intent precision.
    thresholds=[]
    for floor in sorted(set(confidence.tolist())):
        selected=confidence>=floor
        if selected.sum()>=10 and correct[selected].mean()>=.9: thresholds.append((int(selected.sum()),float(floor)))
    floor=max(thresholds)[1] if thresholds else 1.0
    mandatory={'security_incident','compliance_request','data_residency','feature_request','unclear_request','rollback_request'}
    allow=[]
    for intent in model.classes_:
        m=(pred==intent)&(confidence>=floor)
        if intent not in mandatory and m.sum()>=2 and correct[m].mean()>=.85: allow.append(str(intent))
    policy={'version':'development-1','calibration_ready':True,'enabled':True,'intent_threshold':floor,'confidence_threshold':floor,'urgency_threshold':0.0,'retrieval_floor':1.0,'retrieval_ready':False,'eligible_intents':allow,'supported_intents':allow,'sensitive_intents':sorted(mandatory),'selection':{'split':'calibration','minimum_global_sample':10,'minimum_global_precision':.9,'selected_count':int((confidence>=floor).sum()),'selected_intent_accuracy':float(correct[confidence>=floor].mean()) if (confidence>=floor).any() else None,'minimum_per_intent_sample':2,'limitation':'Small per-intent calibration samples; not a quality guarantee'}}
    Path(args.policy).parent.mkdir(parents=True,exist_ok=True); Path(args.policy).write_text(json.dumps(policy,indent=2))
    report['policy_selection']=policy
    manifest={'artifact_id':hash_bytes(json.dumps(files,sort_keys=True).encode()),'created_at':datetime.now(timezone.utc).isoformat(),'data_hash':hash_bytes(raw),'seed':42,'sklearn':sklearn.__version__,'python':platform.python_version(),'sanitizer_hash':hash_bytes(Path('src/guardrails.py').read_bytes()),'features':'sanitized subject + body only','calibration':'sigmoid; disjoint calibration subset; FrozenEstimator','files':files,'split_manifest_hash':hash_bytes((root/'split-manifest.json').read_bytes())}
    (root/'manifest.json').write_text(json.dumps(manifest,indent=2)); (root/'development-report.json').write_text(json.dumps(report,indent=2))
    Path('docs/ml-report-classification.md').write_text('# Development classifier evidence\n\n'+json.dumps({'split':report['split_counts'],'policy':policy,'heldout':{d:report['dimensions'][d]['calibrated_check']['weighted avg'] for d in report['dimensions']}},indent=2)+'\n\nAll reported held-out results are development checks, never supplied validation. See assets/models/development-report.json for per-class results and reliability bins. No subsequent tuning on check results performed. Customer identity/channel/group/history excluded from features. Calibration is measured, not assumed accurate within five percentage points.\n')
    print(json.dumps({'artifact_id':manifest['artifact_id'],'split':report['split_counts'],'eligible_intents':allow,'threshold':floor,'check_accuracy':{d:report['dimensions'][d]['calibrated_check']['accuracy'] for d in report['dimensions']}},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True); p.add_argument('--output',default='assets/models'); p.add_argument('--policy',default='policies/routing.json'); run(p.parse_args())
