from pathlib import Path
exec(Path(__file__).with_name('replay.py').read_text().split("counts=Counter()")[0])
from app.services.research_client import _parse_messages_response
out={}
# Compare every accepted raw output with its captured production validator output.
matches=[]
for s in E['samples']:
 for i,a in enumerate(s['audit']):
  if a['stage']=='llm' and 'raw_structured_payload' in a and i+1<len(s['audit']) and s['audit'][i+1]['stage']=='validator':
   saved=s['audit'][i+1];actual=check(a['raw_structured_payload'],s)
   if 'validated_findings' in saved:
    sig=lambda fs:[(f['category'],f['title'],f['description'],f['status']) for f in fs]
    matches.append(sig(actual['findings'])==sig(saved['validated_findings']))
out['accepted_payload_replays_exact_match']={'matched':sum(matches),'total':len(matches)}
# Same frozen manual sequence, changing only escaped newline representation; keep all guards active.
s=E['samples'][5];a=next(a for a in s['audit'] if a['stage']=='llm');k=kwargs(s)
entry=copy.deepcopy(a['raw_structured_payload']['competitors'][0]);quote=entry['description'].partition('Evidence: "')[2].rpartition('"')[0]
actual=quote.replace('\\n','\n');lines=actual.splitlines();start=next(i for i,l in enumerate(lines) if l.startswith('1. '));block='\n'.join(lines[start:start+4]);assert any(block in sn for sn in k['source_snippets'].values())
base=dict(summary='Internal context only.',confidence=.5,competitors=[entry],market_patterns=[],opportunities=[],risks=[])
for label,b in [('literal_backslash_n',block.replace('\n','\\n')),('actual_newline',block),('atomic_single_step',lines[start+3])]:
 p=copy.deepcopy(base);p['competitors'][0]['description']=f'Evidence: "{b}"';out[label]={'length':len(b),'result':check(p,s)}
# Source-ref evidence attribution checks.
s=E['samples'][1];a=next(a for a in s['audit'] if a['stage']=='llm');e=a['raw_structured_payload']['competitors'][1];quote=r._extract_evidence_quote(e['description']);ids=[ref['provider_source_id'] for ref in e['source_refs']];out['dropbox_overnight']={'quote_in_cited_sources':any(quote in src['snippet'] for src in s['sources'] if src['provider_source_id'] in ids),'cited_ids':ids,'first_failed_guard':'quantifier'}
# A strict protocol failure cannot identify why a tool block is absent.
errs=[]
for stop in ['end_turn','max_tokens','tool_use']:
 try:_parse_messages_response({'content':[{'type':'text','text':'diagnostic placeholder'}],'stop_reason':stop,'usage':{'input_tokens':1,'output_tokens':1}},'submit_research')
 except Exception as ex:errs.append({'supplied_stop_reason':stop,'exception_type':type(ex).__name__,'exception_message':str(ex)})
out['protocol_error_indistinguishability']=errs
out['quote_quantifier_contract']={q:r._quote_contains_quantifier(q) for q in ['select multiple photos','select several photos','select two photos','select 3 photos']}
# Query constrains first permitted namespace, official allowlist accepts other guide namespace.
s=E['samples'][8];rule=r._detect_official_vendor_rule(s['question'],None);url='https://support.apple.com/guide/icloud/upload-and-download-photos-mmb5dbaf376f/icloud'
out['apple_namespace_mismatch']={'actual_query':r._build_official_provider_query(s['question'],None,rule),'guide_allowed_by_strict_validator':r._all_cited_sources_official((('diagnostic-src',url),),{'diagnostic-src':url}),'guide_url_under_search_path':url.startswith('https://support.apple.com/en-us')}
Path('/tmp/phase8-rootcause-controls.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
for key,v in out.items():
 if isinstance(v,dict) and 'result' in v:print(key,{'length':v['length'],'complete_gate':v['result']['complete_gate']})
 else:print(key,json.dumps(v,ensure_ascii=False))
