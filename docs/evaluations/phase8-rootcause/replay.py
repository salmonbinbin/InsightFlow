import sys,json,copy,re,hashlib
from pathlib import Path
from dataclasses import asdict
from collections import Counter
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'backend'))
from app.services import research_service as r
E=json.loads((ROOT/'docs/evaluations/phase8-v4-live-evidence.json').read_text())
out={'kind':'offline frozen evidence replay; no DB/network calls; all production guards enabled','samples':[]}
def kwargs(s,question=None):
 src={x['provider_source_id']:x for x in s['sources']}
 return dict(allowed_provider_source_ids=set(src),allowed_provider_urls={k:v['url'] for k,v in src.items()},source_haystacks={k:' '.join(str(v.get(t) or '') for t in ('title','url','domain','snippet')).lower() for k,v in src.items()},source_snippets={k:v.get('snippet') or '' for k,v in src.items()},validate_evidence_quotes=True,validate_description_grounding=True,research_question=question or s['question'])
def check(p,s,question=None):
 try:
  v=r.validate_research_payload(p,**kwargs(s,question));return {'complete_gate':r._has_external_confirmation(v.findings),'findings':[asdict(f) for f in v.findings]}
 except r.ResearchValidationError as e:return {'error_code':e.code,'actual_error':str(e)}
counts=Counter()
for n,s in enumerate(E['samples'],1):
 row={'n':n,'run_id':s['run_id'],'question':s['question'],'query':s['provider_query'],'attempts':[]}
 for a in s['audit']:
  if a['stage']!='llm':continue
  if 'raw_structured_payload' not in a:
   row['attempts'].append({'protocol_error':a.get('error_type')});continue
  p=a['raw_structured_payload'];at={'replay':check(p,s),'entries':[],'retry_hint':a.get('retry_hint')};k=kwargs(s)
  for field,nfield in [('competitors','name'),('market_patterns','title')]:
   for idx,e in enumerate(p.get(field) or []):
    desc=e.get('description') or '';quote=r._extract_evidence_quote(desc)
    full=desc.partition('Evidence: "')[2].rpartition('"')[0]
    refs=tuple((ref.get('provider_source_id'),ref.get('url')) for ref in e.get('source_refs') or [])
    try:r._validate_source_refs(e.get('source_refs'),allowed_provider_source_ids=k['allowed_provider_source_ids'],allowed_provider_urls=k['allowed_provider_urls']);ref_error=None
    except Exception as ex:ref_error=str(ex)
    checks={'name':r._is_name_supported_by_sources(e.get(nfield,''),refs,k['source_haystacks']),'quote':r._is_evidence_quote_supported(quote or '',refs,k['source_snippets']),'description':r._is_description_grounded_in_snippet(desc,refs,k['source_snippets'],k['allowed_provider_urls']),'quantifier':not r._is_multi_select_question(s['question']) or r._quote_contains_quantifier(quote)}
    first=next((key for key,val in checks.items() if not val),'PASS')
    qdiag=[]
    if not quote:
     if len(full)>200:qdiag.append('quote_length_over_200')
     if not full:qdiag.append('quote_shape_missing')
    if '\\n' in full:qdiag.append('literal_backslash_n')
    if '"' in full:qdiag.append('internal_double_quote')
    if '\n' in full:qdiag.append('actual_newline')
    if full and any(full.lower() in v.lower() for v in k['source_snippets'].values()):qdiag.append('full_quote_exists_in_sources')
    item={'field':field,'idx':idx,'name':e.get(nfield),'description':desc,'first_failed_guard':first,'checks':checks,'quote_diag':qdiag,'full_quote_length':len(full),'extracted_quote':quote,'source_ref_error':ref_error}
    at['entries'].append(item)
  row['attempts'].append(at)
 if row['attempts'] and 'replay' in row['attempts'][-1]:
  rp=row['attempts'][-1]['replay'];row['final_gate_matches_saved_status']=rp.get('complete_gate')==(s['status']=='completed')
  for e in row['attempts'][-1]['entries']:counts[e['first_failed_guard']]+=1
 out['samples'].append(row)
 print(n,s['status'],'attempts',len(row['attempts']),'gate_match',row.get('final_gate_matches_saved_status'))
 for ai,at in enumerate(row['attempts'],1):
  print(' attempt',ai,at.get('protocol_error') or {k:v for k,v in at['replay'].items() if k!='findings'})
  for e in at.get('entries',[]):print(' ',e['field'],e['idx'],e['first_failed_guard'],'checks',e['checks'],'len',e['full_quote_length'],e['quote_diag'])
# Counterfactuals only change supplied data, never production guards.
s=E['samples'][5];source=next(x for x in s['sources'] if '6193313' in x['url']);line='4. Tap Back up now.'
assert line in source['snippet']
def candidate(description):return dict(summary='Internal context only.',confidence=.5,competitors=[dict(name='Google Photos',description=description,confidence=.5,source_refs=[{k:source[k] for k in ('provider_source_id','url','title')}])],market_patterns=[],opportunities=[],risks=[])
cf={}
cf['atomic_correct_step_same_question']=check(candidate(f'Evidence: "{line}"'),s)
cf['identical_step_multiselect_question']=check(candidate(f'Evidence: "{line}"'),s,E['samples'][0]['question'])
cf['same_quote_unsupported_negation']=check(candidate(f'No support. Evidence: "{line}"'),s)
# Relevance: replay the accepted automatic-backup quote against the actual manual question.
line2='Turn on Backup while roaming.'
assert line2 in source['snippet']
cf['automatic_quote_against_manual_question']=check(candidate(f'Evidence: "{line2}"'),s)
# Quote parser grammar: synthetic representation from exact real source strings.
for n,label in [(0,'google_photos'),(4,'google_drive')]:
 sample=E['samples'][n]
 for src in sample['sources']:
  lines=[ln for ln in src.get('snippet','').splitlines() if re.match(r'^[1-4]\. ',ln)]
  if len(lines)>=4:
   block='\n'.join(lines[:4]);one=lines[0]
   if block in src['snippet']:
    test_sample=copy.deepcopy(sample);test_sample['question']='What are the official upload steps?'
    def payload(q):return dict(summary='Internal context only.',confidence=.5,competitors=[dict(name=sample['product'],description=f'Evidence: "{q}"',confidence=.5,source_refs=[{k:src[k] for k in ('provider_source_id','url','title')}])],market_patterns=[],opportunities=[],risks=[])
    cf[label+'_exact_multiline']={'quote_length':len(block),'result':check(payload(block),test_sample)}
    cf[label+'_atomic_first_step']={'quote_length':len(one),'result':check(payload(one),test_sample)}
    break
out['final_first_failed_counts']=dict(counts);out['counterfactuals']=cf
Path('/tmp/phase8-rootcause-replay.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print('COUNTS',dict(counts))
for k,v in cf.items():print('CF',k,json.dumps(v,ensure_ascii=False)[:700])
