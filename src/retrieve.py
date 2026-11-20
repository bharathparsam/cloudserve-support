"""Chroma cosine index backed by immutable, offset-addressed MiniLM passages."""
import hashlib,json,os
from pathlib import Path
from .contracts import SourcePassage
MODEL='sentence-transformers/all-MiniLM-L6-v2'
REVISION='1110a243fdf4706b3f48f1d95db1a4f5529b4d41'

def chunks(docs,tokenizer,size=600,overlap=80):
    result=[]
    for doc in docs:
        content=doc['content']; start=0
        while start<len(content):
            end=min(len(content),start+size)
            if end<len(content):
                boundary=content.rfind('\n',start+size//2,end)
                if boundary>start: end=boundary+1
            # SentenceTransformer's effective maximum is 256 incl special tokens.
            while len(tokenizer.encode(content[start:end],add_special_tokens=True))>256:
                end=start+max(1,(end-start)*9//10)
            text=content[start:end]
            cid=doc['doc_id']+'-'+hashlib.sha256(f'{start}:{end}:{text}'.encode()).hexdigest()[:16]
            result.append({'doc_id':doc['doc_id'],'chunk_id':cid,'title':doc['title'],'content':text,'start':start,'end':end})
            if end==len(content): break
            start=max(start+1,end-overlap)
    return result

def encode_queries(model,texts):
    """Embed every query token, averaging normalized token windows without omission."""
    import numpy as np
    vectors=[]
    for text in texts:
        ids=model.tokenizer.encode(text,add_special_tokens=False)
        windows=[model.tokenizer.decode(ids[i:i+254],skip_special_tokens=True) for i in range(0,len(ids),254)] or ['']
        e=model.encode(windows,normalize_embeddings=True,show_progress_bar=False)
        mean=np.mean(e,axis=0); norm=np.linalg.norm(mean)
        vectors.append(mean/norm if norm else mean)
    return np.asarray(vectors)

class Retriever:
    def __init__(self,corpus_path,chroma_path,**kwargs):
        self.ready=False; self.metadata={}; self.collection=None; self.model=None; self.passages={}
        self.corpus_path=Path(corpus_path); self.chroma_path=Path(chroma_path)
        try:
            manifest_path=self.corpus_path.parent/'index-manifest.json'
            self.metadata=json.loads(manifest_path.read_text())
            actual=hashlib.sha256(self.corpus_path.read_bytes()).hexdigest()
            if actual!=self.metadata['corpus_hash']: raise ValueError('Corpus hash mismatch')
            import chromadb
            from sentence_transformers import SentenceTransformer
            cache=Path(os.environ.get('HF_HOME',str(Path(__file__).resolve().parents[1]/'storage/cache/huggingface')))/'hub'
            snapshot=cache/('models--'+MODEL.replace('/','--'))/'snapshots'/self.metadata['embedding_revision']
            local_only=kwargs.get('local_files_only',snapshot.is_dir())
            self.model=SentenceTransformer(MODEL,cache_folder=str(cache),revision=self.metadata['embedding_revision'],device='cpu',local_files_only=local_only)
            self.client=chromadb.PersistentClient(path=str(self.chroma_path))
            passages=json.loads((self.corpus_path.parent/'passages.json').read_text())
            if hashlib.sha256(json.dumps(passages,sort_keys=True).encode()).hexdigest()!=self.metadata['collection_fingerprint']: raise ValueError('Passage fingerprint mismatch')
            self.passages={p['chunk_id']:p for p in passages}
            import numpy as np
            ep=self.corpus_path.parent/'passage-embeddings.npy'
            if hashlib.sha256(ep.read_bytes()).hexdigest()!=self.metadata['embeddings_hash']: raise ValueError('Embedding artifact mismatch')
            self.collection=self.client.get_or_create_collection(self.metadata['collection'],metadata={'hnsw:space':'cosine'})
            if self.collection.count()!=len(self.passages):
                passages=list(self.passages.values()); embeddings=np.load(ep,allow_pickle=False)
                self.collection.upsert(ids=[p['chunk_id'] for p in passages],embeddings=embeddings.tolist(),documents=[p['content'] for p in passages],metadatas=[{k:p[k] for k in ('doc_id','title','start','end')} for p in passages])
            if self.collection.count()!=len(self.passages): raise ValueError('Index incomplete')
            self.ready=True
        except Exception as e:
            self.metadata={**self.metadata,'status':'unavailable','reason':'Embedding cache/index unavailable; run scripts.index', 'error_type':type(e).__name__}
    def search(self,text,k=4):
        if not self.ready or not text.strip(): return []
        e=encode_queries(self.model,[text])
        self.last_query_metadata={'tokens':len(self.model.tokenizer.encode(text,add_special_tokens=False)),'window_tokens':254,'strategy':'all_windows_normalized_mean','truncated':False}
        r=self.collection.query(query_embeddings=e.tolist(),n_results=len(self.passages),include=['distances'])
        found=[]
        for cid,distance in zip(r['ids'][0],r['distances'][0]):
            p=self.passages[cid]; found.append(SourcePassage(**p,corpus_hash=self.metadata['corpus_hash'],score=float(1-distance)))
        self.last_candidates=sorted(found,key=lambda p:(-p.score,p.doc_id,p.chunk_id))[:max(1,k)]
        self.last_query_metadata['raw_cosine_distances']=[{'chunk_id':p.chunk_id,'distance':float(1-p.score),'similarity':p.score} for p in self.last_candidates]
        floor=float(self.metadata['retrieval_floor'])
        return [p for p in self.last_candidates if p.score>=floor]
    def resolve(self,chunk_id):
        p=self.passages.get(chunk_id)
        return SourcePassage(**p,corpus_hash=self.metadata['corpus_hash'],score=0.0) if p else None
