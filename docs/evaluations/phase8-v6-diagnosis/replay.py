"""Read-only offline controls; no HTTP, LLM, database, production edits or credentials."""
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
from dataclasses import replace
import json, collections, argparse
from app.services import research_service as api
from app.services.research_selection import build_catalogs
from app.services.research_claims import materialize_facts, materialize_analyses, _platform_tokens, ClaimError
from app.services.research_coverage import question_anchors, prepare_plan, evaluate, _comparison
from app.services.research_tasks import TaskContractError, QuestionTaskPlan, TaskItem, Subject
from app.agents.prompts import research_agent_v6
ROOT=Path(__file__).resolve().parents[3];A=ROOT/'docs/evaluations/phase8-v6'
parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);OUT=parser.parse_args().output_dir
OUT.mkdir(parents=True,exist_ok=True)
if (OUT/'replay-results.json').exists():raise FileExistsError('Use a fresh output directory; never overwrite frozen evidence.')
orig=json.loads((A/'original/live-research.json').read_text())['samples'];ext=json.loads((A/'extended/live-research.json').read_text())['samples'];results={}
def unpack(sample):
 sources=[SimpleNamespace(**s) for s in sample['sources']]
 rid=UUID(sample['run_id']);pid=uuid4()
 for source in sources:source.run_id=rid
 cats,entries=build_catalogs(sources)
 return sources,cats,entries,rid,pid
# Same exact bytes, different whole-question quantity requirement.
s,c,e,r,p=unpack(orig[2]);facts=[]
for question in [orig[2]['question'],orig[7]['question']]:
 key_ids=[i for i,v in e.items() if v["source"].provider_source_id==s[0].provider_source_id and any(t in v["unit"].text for t in ["Choose Existing","Browse to the pictures","In the OneDrive app"])]
 fs=materialize_facts(key_ids,e,research_question=question,run_id=r,project_id=p,api=api)
 chosen=[f for f in fs if any(t in f.description for t in ['Choose Existing','Browse to the pictures','In the OneDrive app'])]
 facts.append({'question':question,'same_snapshot':True,'key_steps':[{'text':f.description,'status':f.status,'guard':f.evidence_location.get('first_failed_guard')} for f in chosen]})
results['quantity_applied_to_every_quote']=facts
assert all(x['status']=='not_verified' for x in facts[0]['key_steps']) and all(x['status']=='confirmed' for x in facts[1]['key_steps'])
# Exact official Windows quote on a mobile question has no recognized OS scope.
s,c,e,r,p=unpack(ext[5]);eid=next(i for i,v in e.items() if 'Windows devices does not currently support video uploads' in v['unit'].text)
f=materialize_facts([eid],e,research_question=ext[5]['question'],run_id=r,project_id=p,api=api)[0]
results['windows_mobile_scope']={'quote':f.description,'question':ext[5]['question'],'recognized_quote_platforms':sorted(_platform_tokens(f.description)),'status':f.status};assert f.status=='confirmed'
# Contract wire schema permits scenario to 500 while materializer caps 200.
candidate={'category':'risk','claim_kind':'inference','title':'移动端上传流程风险','description':'可以在该手动上传场景中进一步验证误选风险。','scenario':'x'*300,'supporting_evidence_ids':[eid]}
try:materialize_analyses([candidate],[f],run_id=r,project_id=p);err='accepted'
except ClaimError as error:err=str(error)
results['schema_runtime_mismatch']={'field':'analyses[].scenario','wire_max':research_agent_v6.TOOL_SCHEMA['properties']['analyses']['items']['properties']['scenario']['maxLength'],'runtime_300_chars':err};assert err=='scenario_too_long'
# Schema permits an analysis task, but evaluate has no semantic analysis predicate.
q='Compare manual photo upload entry points. Give patterns, opportunities and risks.'
plan=QuestionTaskPlan(q,[TaskItem('analysis-1',(q.index('Give'),len(q)-1),'analysis','core',Subject(),'upload',())]);report=evaluate(plan,[{'task_id':'analysis-1','evidence_ids':[eid]}],[f],e,c,{x.provider_source_id for x in s});results['analysis_task_not_automatically_completable']={'anchors':question_anchors(q),'report':report};assert not report['overall_completable']
# Lexical task contract control: semantically common alias does not authorize original span.
q='According to official Microsoft Support, how does an iPhone user upload photos? Cite only official Microsoft Support.'
anchors=question_anchors(q)
task={'id':'t1','question_anchor_id':anchors[0]['id'],'kind':'procedure','role':'core','subject':{'product':None,'platform':'iOS','entry':'app'},'action':'upload','constraints':[],'answer_mode':'explicit_official_evidence'}
try:prepare_plan(q,[task]);err='accepted'
except TaskContractError as error:err=error.code
results['lexical_subject_control']={'question':q,'subject_candidate':'iOS','error':err};assert err=='task_subject_not_in_question'
# Actual #9 inventory: distinguish missing source/catalog/offer/selection stages.
s,c,e,r,p=unpack(orig[8]);offered={v['evidence_id'] for v in orig[8]['execution_trace']['evidence_locations']};selected={i for event in orig[8]['execution_trace']['events'] for i in event.get('selected_evidence_ids',[])};key=[]
for i,v in e.items():
 t=v['unit'].text
 if ('On a phone or tablet:' in t and 'Photo Library' in t) or ('If you want to upload photos to your iCloud Shared Photo Library' in t):key.append({'evidence_id':i,'quote':t,'catalog_present':True,'offered':i in offered,'selected_in_any_recorded_valid_stage':i in selected,'url':v['source'].url})
results['original_9_stage_localization']={'run_id':orig[8]['run_id'],'reconstructed_catalog_units':len(e),'offered':len(offered),'target_units':key};assert len([x for x in key if 'mmb5dbaf376f' in x['url']])==2
# Replay candidate selection: more confirmed snippets outrank the actual requested answer.
rounds=[]
for event in orig[8]['execution_trace']['events']:
 if event['stage']=='llm' and 'selected_evidence_ids' in event:
  fs=materialize_facts(event['selected_evidence_ids'],e,research_question=orig[8]['question'],run_id=r,project_id=p,api=api)
  coverage=event['coverage'];score=(coverage['overall_completable'],sum(x['status']=='covered' for x in coverage['items'] if x['role']=='core'),sum(f.status=='confirmed' for f in fs),0)
  rounds.append({'attempt':event['attempt'],'selection_score':score,'contains_phone_options':any('On a phone or tablet:' in f.description and 'Photo Library' in f.description for f in fs),'contains_shared_destination':any('If you want to upload photos to your iCloud Shared Photo Library' in f.description for f in fs)})
assert rounds[0]['selection_score']>rounds[1]['selection_score'] and not rounds[0]['contains_phone_options'] and rounds[1]['contains_phone_options'] and rounds[1]['contains_shared_destination']
results['original_9_best_candidate_regression']={'run_id':orig[8]['run_id'],'rounds':rounds,'selected_attempt_by_current_score':max(rounds,key=lambda x:x['selection_score'])['attempt'],'conclusion':'第三轮已选中关键手机/Shared Library答案，但两轮任务均失败后按confirmed数量7>6选回第二轮，最终丢失目标答案。'}

# Source policy is re-added as a business condition by the second parser.
q=ext[3]['question'];anchors=question_anchors(q);task={'id':'t1','question_anchor_id':anchors[0]['id'],'kind':'procedure','role':'core','subject':{'product':'Google Drive','platform':'Android','entry':'app'},'action':'upload','constraints':[],'answer_mode':'explicit_official_evidence'};plan2=prepare_plan(q,[task]);clauses=[q[slice(*x.question_span)] for x in plan2.unresolved];assert any(x.startswith('only official') for x in clauses)
results['citation_policy_becomes_business_condition']={'question':q,'unresolved_clauses':clauses}
# Multilingual search loses the requested manual operation before any provider result.
results['chinese_query_term_loss']={'question':ext[6]['question'],'scanner_platforms_and_actions':api._scan_semantic_terms(ext[6]['question']),'actual_queries':[x['query'] for x in ext[6]['execution_trace']['events'] if x['stage']=='search']}
# Version constraints are unconditionally blocked even with matching version facts.
s,c,e,r,p=unpack(ext[2]);eid=next(i for i,v in e.items() if 'If you want to upload photos to your iCloud Shared Photo Library' in v['unit'].text);f=materialize_facts([eid],e,research_question=ext[2]['question'],run_id=r,project_id=p,api=api)[0]
from app.services.research_tasks import Constraint
q='Where should users upload photos on iOS 16.1?';task=TaskItem('versioned-destination',(0,len(q)-1),'destination','core',Subject(),'upload',(Constraint('version','iOS 16.1'),));pr=QuestionTaskPlan(q,[task]);vr=evaluate(pr,[{'task_id':task.id,'evidence_ids':[eid]}],[f],e,c,{x.provider_source_id for x in s});assert not vr['overall_completable'];results['version_constraint_unconditional_block']={'question':q,'matching_fact':f.description,'report':vr}
# General comparison is implemented as manual-versus-automatic only.
results['generic_comparison_specialized_to_backup_mode']={'source_function':'research_coverage._comparison','predicate':'manual context + numbered step AND automatic upload/backup/save sentence','consequence':'Two-product differences unrelated to automatic backup have no general comparison predicate.'}

# Counterpart wording and traces expose rejected task candidates not saved.
results['task_candidate_observability']={'archived_llm_event_fields':sorted(set(k for event in orig[8]['execution_trace']['events'] if event['stage']=='llm' for k in event)),'candidate_task_payload_archived':any('tasks' in event for event in orig[8]['execution_trace']['events'] if event['stage']=='llm')}
# Evidence IDs for the real browser market pattern are legal but lack one stated premise.
d=json.loads((A/'browser-final4-analysis-evidence.json').read_text());by={x['id']:x for x in d['findings']['items']};analysis=next(x for x in by.values() if x['category']=='market_pattern');results['market_pattern_support_texts']={'finding_id':analysis['id'],'description':analysis['description'],'supports':[by[i]['description'] for i in analysis['supporting_finding_ids']]}
# Downstream parser accepts known risk with allowed IDs without proving causal premise.
from app.services.requirement_prd import _parse_requirement_payload, _parse_prd_payload
req=json.loads((A/'workflow-final4-requirement.json').read_text());keys=['name','problem','target_user','user_story','solution_hypothesis','success_metrics','risks','evidence_references'];parsed=_parse_requirement_payload({k:req[k] for k in keys},allowed_evidence_ids={'F001','F002','F003'});warnings=[x if isinstance(x,dict) else x.model_dump() for x in parsed[-1]]
prd=json.loads((A/'workflow-final4-prd.json').read_text());_,prd_warnings=_parse_prd_payload(prd['content'],allowed_evidence_ids={'F001','F002','F003'})
results['downstream_known_risk_id_only']={'requirement_id':req['id'],'known_risks':[x for x in req['risks'] if x['category']=='known'],'requirement_parser_accepted':True,'requirement_warnings':warnings,'prd_parser_accepted':True,'prd_warnings':[x if isinstance(x,dict) else x.model_dump() for x in prd_warnings],'raw_text_context':'F001/F003只证明一次只能选一张/上传速度尚可，没有证明审核或后端策略原因。'}
assert all(x.get('code')!='known_risk_evidence_missing' for x in warnings)

(OUT/'replay-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in results.items() if k not in ['analysis_task_not_automatically_completable','market_pattern_support_texts','task_candidate_observability']},ensure_ascii=False,indent=2));print('READ_ONLY_OFFLINE_CONTROLS_PASSED')
