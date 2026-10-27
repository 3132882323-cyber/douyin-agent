'use strict';
const assert=require('node:assert/strict'),Queue=require('./product-package-queue.js');
const id='1000000000000000001',items=[{id,name:'合成商品'}];
const product={source:{platform:'doudian',source_kind:'doudian_store_product',product_id:id,store_key:'archive_'+'a'.repeat(64),context_key:'synthetic-scope',captured_at:'2026-10-06T00:00:00.000Z'},title:'合成商品'};
const clone=value=>JSON.parse(JSON.stringify(value));
function history(p){return{revision:1,historical:true,execution_authorized:false,groups:[{key:JSON.stringify([p.source.source_kind,p.source.product_id,p.source.store_key,'']),anonymous_slot:'',snapshots:[{saved_at:'2026-10-06T00:01:00.000Z',product:clone(p)}]}]};}
(async()=>{
 let saves=0,reads=0,current=true,version=clone(product);
 const queue=Queue.create({readModel:async()=>({product:clone(product)}),save:async()=>{saves++;throw Error('模拟保存回执丢失');},readHistory:async()=>{reads++;return history(version);},isCurrent:()=>current});
 assert.equal((await queue.start(items)).items[0].state,'needs_review');
 assert.equal(queue.canStart(),false);
 await assert.rejects(queue.reconcile({confirmed:false}));
 version.title='另一合成版本';
 assert.equal((await queue.reconcile({confirmed:true})).items[0].state,'needs_review');
 assert.equal(saves,1);
 await assert.rejects(queue.start(items));
 version=clone(product);
 const recovered=await queue.reconcile({confirmed:true});
 assert.equal(recovered.items[0].state,'saved');
 assert.equal(recovered.items[0].recovery,'current_version_present_not_io_attributed');
 assert.match(recovered.items[0].message,/不能归因/);
 assert.equal(saves,1);
 assert.equal(reads,2);
 assert.equal(queue.hasReview(),false);
 current=false;
 assert.equal(queue.canStart(),false);

 const draft=clone(product);
 draft.source.source_kind='doudian_draft_product';
 let draftSaves=0,draftReads=0,draftCurrent=true,draftVersion=clone(draft);
 const draftQueue=Queue.create({
  readModel:async()=>({product:clone(draft)}),
  save:async()=>{draftSaves++;throw Error('模拟草稿保存回执丢失');},
  readHistory:async()=>{draftReads++;return history(draftVersion);},
  isCurrent:()=>draftCurrent
 });
 assert.equal((await draftQueue.start(items)).items[0].state,'needs_review');
 assert.equal(draftQueue.canReconcile(),true);
 assert.equal(draftSaves,1);
 await assert.rejects(draftQueue.start(items),/未知保存仍待核对/);
 await assert.rejects(draftQueue.retryFailed(),/未知保存仍待核对/);

 draftVersion.source.store_key='archive_'+'b'.repeat(64);
 const otherScope=await draftQueue.reconcile({confirmed:true});
 assert.equal(otherScope.items[0].state,'needs_review');
 assert.equal(otherScope.items[0].recovery,undefined);
 assert.match(otherScope.items[0].message,/未找到完全相同版本/);
 assert.equal(draftReads,1);
 assert.equal(draftSaves,1);
 assert.equal(draftQueue.canStart(),false);

 draftCurrent=false;
 await assert.rejects(draftQueue.reconcile({confirmed:true}),/来源已变化/);
 assert.equal(draftReads,1);
 draftCurrent=true;
 draftVersion=clone(draft);
 const draftRecovered=await draftQueue.reconcile({confirmed:true});
 assert.equal(draftRecovered.items[0].state,'saved');
 assert.equal(draftRecovered.items[0].recovery,'current_version_present_not_io_attributed');
 assert.match(draftRecovered.items[0].message,/不能归因/);
 assert.equal(draftSaves,1);
 assert.equal(draftReads,2);
 assert.equal(draftQueue.hasReview(),false);

 const incompleteDraft=clone(draft);
 incompleteDraft.source.context_key='';
 let incompleteSaves=0,incompleteReads=0;
 const incompleteQueue=Queue.create({
  readModel:async()=>({product:clone(incompleteDraft)}),
  save:async()=>{incompleteSaves++;throw Error('模拟草稿保存回执丢失');},
  readHistory:async()=>{incompleteReads++;return history(incompleteDraft);},
  isCurrent:()=>true
 });
 assert.equal((await incompleteQueue.start(items)).items[0].state,'needs_review');
 assert.equal(incompleteQueue.canReconcile(),false);
 await assert.rejects(incompleteQueue.reconcile({confirmed:true}),/没有完整本店私有版本依据/);
 await assert.rejects(incompleteQueue.start(items),/未知保存仍待核对/);
 assert.equal(incompleteReads,0);
 assert.equal(incompleteSaves,1);
 console.log('public queue recovery: synthetic store and draft cases passed; no real storage or merchant requests');
})().catch(error=>{console.error(error);process.exitCode=1;});
