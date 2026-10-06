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
 console.log('public queue recovery: 13 assertions; no real storage or merchant requests');
})().catch(error=>{console.error(error);process.exitCode=1;});
