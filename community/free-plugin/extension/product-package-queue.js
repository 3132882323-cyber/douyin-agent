/* Explicit, page-bound product reads and local saves. No platform writes. */
(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.DianProductPackageQueue=api;})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const LIMIT=100;
  function create({readModel,save,readHistory,isCurrent,onChange=()=>{},readTimeoutMs=30000,saveTimeoutMs=30000,reconcileTimeoutMs=30000}){
    if([readModel,save,isCurrent].some(fn=>typeof fn!=="function"))throw Error("商品读取组件未就绪。");
    if([readTimeoutMs,saveTimeoutMs,reconcileTimeoutMs].some(n=>!Number.isSafeInteger(n)||n<1||n>120000)||readHistory!==undefined&&typeof readHistory!=="function")throw Error("商品处理超时设置或只读核对配置无效。");
    let task=null,working=false,disposed=false,pauseRequested=false,stopRequested=false,consecutiveFailures=0;
    let unsettled=0;
    const recoveries=new Map();let reconciling=false,reconcileCancelled=false,reconcileGeneration=0;
    function safeCopy(value,depth=0,budget={nodes:0}){if(depth>20||++budget.nodes>100000)throw Error('资料超过本机核对结构上限。');if(value===null||typeof value==='boolean')return value;if(typeof value==='string'){if(value.length>12000)throw Error('资料文本超过核对上限。');return value;}if(typeof value==='number'&&Number.isFinite(value)&&Math.abs(value)<=Number.MAX_SAFE_INTEGER)return value;if(Array.isArray(value)){if(value.length>5000||Reflect.ownKeys(value).length!==value.length+1)throw Error('资料数组无效。');return Array.from({length:value.length},(_,i)=>{const d=Object.getOwnPropertyDescriptor(value,String(i));if(!d||!Object.hasOwn(d,'value'))throw Error('资料访问器或缺项不能核对。');return safeCopy(d.value,depth+1,budget);});}if(value&&typeof value==='object'&&[Object.prototype,null].includes(Object.getPrototypeOf(value))){const out=Object.create(null);for(const key of Reflect.ownKeys(value).sort()){const d=Object.getOwnPropertyDescriptor(value,key);if(typeof key!=='string'||key.length>128||!Object.hasOwn(d,'value'))throw Error('资料访问器不能核对。');out[key]=safeCopy(d.value,depth+1,budget);}return out;}throw Error('资料不是完整可核对的数据。');}
    function expectedProduct(value,id){const product=safeCopy(value),s=product.source;if(!s||s.platform!=='doudian'||s.product_id!==id||!['doudian_store_product','doudian_edit_product'].includes(s.source_kind)||!/^archive_[a-f0-9]{64}$/.test(s.store_key||'')||typeof s.context_key!=='string'||!s.context_key||!Number.isFinite(Date.parse(s.captured_at)))throw Error('商品本店来源或版本范围不足，不能自动核对未知保存。');const serialized=JSON.stringify(product);if(new TextEncoder().encode(serialized).length>4*1024*1024)throw Error('完整商品超过本次核对4MiB上限，未截断。');return{serialized,product_id:id,source_kind:s.source_kind,store_key:s.store_key,captured_at:s.captured_at,group_key:JSON.stringify([s.source_kind,id,s.store_key,''])};}
    function historyMatches(history,expected){const h=safeCopy(history);if(h.historical!==true||h.execution_authorized!==false||!Number.isSafeInteger(h.revision)||h.revision<0||!Array.isArray(h.groups)||h.groups.length>100)throw Error('资料库只读回执格式未通过。');const keys=new Set();let found=false;for(const group of h.groups){if(typeof group.key!=='string'||keys.has(group.key)||typeof group.anonymous_slot!=='string'||!Array.isArray(group.snapshots)||!group.snapshots.length||group.snapshots.length>8)throw Error('资料库分组或版本无效。');keys.add(group.key);let previous=Infinity;for(const row of group.snapshots){const p=row.product,s=p?.source,saved=Date.parse(row.saved_at),captured=Date.parse(s?.captured_at);if(!s||!Number.isFinite(saved)||saved>previous||!Number.isFinite(captured)||captured>saved+60000||group.key!==JSON.stringify([s.source_kind,s.product_id,s.store_key||'',s.store_key?'':group.anonymous_slot])||s.store_key&&group.anonymous_slot!==''||!s.store_key&&!/^anonymous-\d+$/.test(group.anonymous_slot))throw Error('资料库版本来源未吻合。');previous=saved;if(group.key===expected.group_key&&s.product_id===expected.product_id&&s.source_kind===expected.source_kind&&s.store_key===expected.store_key&&s.captured_at===expected.captured_at&&JSON.stringify(p)===expected.serialized)found=true;}}return found;}
    async function bounded(operation,ms,code,onTimeout=()=>{}){
      let timer;unsettled++;
      let pending;try{pending=Promise.resolve(operation());}catch(error){unsettled--;throw error;}
      pending.then(()=>{unsettled--;notify();},()=>{unsettled--;notify();});
      try{return await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>{onTimeout();const error=Error(code==="PACKAGE_READ_TIMEOUT"?"商品读取超时，已停止后续；晚到资料不会保存。":code==="PACKAGE_RECONCILE_TIMEOUT"?"资料库核对超时，旧结果不会采用；没有重复保存。":"本机保存超时，结果待核对；不会自动重复保存。");error.code=code;reject(error);},ms);})]);}
      finally{clearTimeout(timer);}
    }
    function snapshot(){return task?JSON.parse(JSON.stringify(task)):null;}
    function notify(){if(!disposed)try{onChange(snapshot());}catch{/* UI failure cannot replay a committed save. */}}
    function current(){try{return !disposed&&isCurrent()===true;}catch{return false;}}
    function finishStopped(message){task.state="stopped";task.message=message;for(const item of task.items)if(item.state==="waiting")item.state="not_started";notify();}
    async function run(){
      if(working||disposed||!task)return snapshot();
      working=true;task.state="running";task.message="正在读取并保存…";notify();
      try{
        while(task.next<task.items.length){
          if(stopRequested||!current()){finishStopped(stopRequested?"已停止。已经保存的资料会保留。":"页面或店铺已变化，已停止剩余商品。");break;}
          if(pauseRequested){task.state="paused";task.message="已暂停，可继续剩余商品。";notify();break;}
          const item=task.items[task.next];item.state="reading";item.message="正在读取商品资料";notify();
          if(stopRequested||!current()){item.state="not_started";item.message="读取开始前已停止，没有请求商品资料。";task.next++;finishStopped("页面已变化或任务已停止，剩余商品未处理。");break;}
          let product;
          try{const response=await bounded(()=>readModel(item.id),readTimeoutMs,"PACKAGE_READ_TIMEOUT");product=safeCopy(response?.product);if(!product||product.source?.product_id!==item.id)throw Error("读取结果与所选商品不一致。");}
          catch(error){
            if(stopRequested||!current()){item.state="not_saved";item.message="读取已停止，没有开始保存。";task.next++;finishStopped("页面已变化或任务已停止，剩余商品未处理。");break;}
            item.state="failed";item.message=String(error?.message||"读取失败").slice(0,300);task.next++;consecutiveFailures++;
            if(error?.code==="PACKAGE_READ_TIMEOUT"){finishStopped("读取超时，已停止本批；旧请求结束前不会启动另一批，晚到资料不会采用。");break;}
            if(["LOGIN_OR_PERMISSION","PAGE_CHANGED","MODEL_SENDER_REJECTED"].includes(error?.code)){finishStopped("当前页面或读取权限已变化，请检查抖店页面。");break;}
            if(consecutiveFailures>=3){task.state="paused";pauseRequested=true;task.message="连续 3 件读取失败，已暂停。检查页面后可继续。";notify();break;}
            notify();continue;
          }
          if(stopRequested||!current()){item.state="not_saved";item.message="读取已结束，未开始保存。";task.next++;finishStopped("已停止。当前未保存项不会继续写入资料库。");break;}
          item.state="saving";item.message="正在保存到本机";notify();
          if(stopRequested||!current()){item.state="not_saved";item.message="保存开始前已停止，没有请求写入资料库。";task.next++;finishStopped("当前商品未开始保存，已停止后续处理。");break;}
          let saveLive=true,expected=null,recoveryReason='';try{expected=expectedProduct(product,item.id);}catch(e){recoveryReason=e.message;}
          try{
            const receipt=await bounded(()=>save(product,()=>saveLive&&current()&&!stopRequested),saveTimeoutMs,"PACKAGE_SAVE_TIMEOUT",()=>{saveLive=false;});
            saveLive=false;
            if(receipt?.historical!==true||receipt.execution_authorized!==false)throw Error("保存结果尚未确认。");
            item.state="saved";item.message="已保存到资料库";consecutiveFailures=0;task.next++;notify();
          }catch(error){
            if(expected)recoveries.set(item,expected);else recoveries.set(item,{unavailable:recoveryReason});
            item.state="needs_review";item.message=String(error?.message||"保存结果待核对，请查看资料库。").slice(0,220)+(expected?'':' '+recoveryReason);task.next++;
            finishStopped("保存结果待核对，已停止后续商品。请先查看资料库，不会自动重复保存。");break;
          }finally{saveLive=false;}
        }
        if(task.next===task.items.length&&task.state!=="stopped"){
          task.state="completed";task.message=task.items.some(item=>item.state==="failed")?"本批已结束，读取失败的商品可以单独重试。":"本批商品已保存，可到资料库查看。";notify();
        }
      }finally{working=false;notify();}
      return snapshot();
    }
    async function start(items){
      if(disposed)throw Error("商品助手已关闭。");
      if(unsettled)throw Error("上一请求尚未结束，不能重复启动。请先等待或核对资料库；不会自动重试。");
      if(reconciling||task?.items.some(item=>item.state==='needs_review'))throw Error('当前未知保存仍待核对，不能丢弃后重新保存。');
      if(working||task&&["running","pausing","paused","stopping"].includes(task.state))throw Error("请先结束当前批次。");
      if(!Array.isArray(items)||!items.length||items.length>LIMIT||new Set(items.map(item=>item?.id)).size!==items.length
        ||Array.from({length:items.length},(_,index)=>index).some(index=>!Object.hasOwn(items,index))
        ||items.some(item=>typeof item?.id!=="string"||!/^\d{10,30}$/.test(item.id)||typeof item.name!=="string"))throw Error(`每批请选择 1–${LIMIT} 件明确的商品。`);
      if(!current())throw Error("页面或店铺已变化，请重新读取商品列表。");
      recoveries.clear();pauseRequested=false;stopRequested=false;consecutiveFailures=0;
      task={state:"running",next:0,message:"",items:items.map(item=>({id:item.id,name:item.name.slice(0,4000),state:"waiting",message:"等待处理"}))};
      return run();
    }
    function pause(){if(!working||!task||stopRequested||pauseRequested)return false;pauseRequested=true;task.state="pausing";task.message="当前商品处理完后暂停。";notify();return true;}
    async function resume(){if(disposed||working||task?.state!=="paused")return snapshot();if(!current()){finishStopped("页面或店铺已变化，请重新选择商品。");return snapshot();}pauseRequested=false;consecutiveFailures=0;return run();}
    async function reconcile({confirmed=false}={}){if(confirmed!==true)throw Error('请明确确认仅只读核对资料库，不会重复保存。');if(disposed||working||unsettled||reconciling||!current())throw Error('旧请求尚未结束或来源已变化，不能核对。');if(typeof readHistory!=='function')throw Error('资料库只读核对连接尚未接入。');const ownerTask=task,items=task?.items.filter(i=>i.state==='needs_review')||[];if(!items.length)throw Error('没有待核对保存。');if(!items.some(i=>recoveries.get(i)?.serialized))throw Error('没有完整本店私有版本依据，不能自动核对；请查看资料库。');const rev=++reconcileGeneration;reconciling=true;reconcileCancelled=false;working=true;task.message='正在只读核对资料库；没有再次保存。';notify();try{if(!current()||reconcileCancelled)throw Error('核对已停止或来源变化。');const history=await bounded(()=>readHistory(),reconcileTimeoutMs,'PACKAGE_RECONCILE_TIMEOUT',()=>{reconcileCancelled=true;});if(disposed||task!==ownerTask||rev!==reconcileGeneration)return snapshot();if(!current()||reconcileCancelled)throw Error('范围已变化或核对已停止，未采用回执。');const results=items.map(item=>{const expected=recoveries.get(item);if(!expected||expected.unavailable)return{item,found:false,message:expected?.unavailable||'缺少本次私有版本依据，仍需人工核对。'};const found=historyMatches(history,expected);return{item,found,message:found?'资料库当前存在完全相同版本；不能归因本次保存请求。':'本次读取未找到完全相同版本，仍待核对；不会重新保存。'};});if(!current()||reconcileCancelled||disposed||task!==ownerTask)throw Error('核对来源已变化。');for(const r of results){r.item.message=r.message;if(r.found){r.item.state='saved';r.item.recovery='current_version_present_not_io_attributed';recoveries.delete(r.item);}}task.message='只读核对结束：当前存在不等于本次IO归因；未匹配项仍待核对。';notify();}catch(e){if(!disposed&&task===ownerTask&&rev===reconcileGeneration){task.message=String(e?.message||'只读核对未完成，仍待核对。').slice(0,300);notify();}}finally{reconciling=false;working=false;notify();}return snapshot();}
    function stop(){if(reconciling){if(reconcileCancelled)return false;reconcileCancelled=true;task.message='已停止采用核对结果；底层只读请求仍可能未结束。';notify();return true;}if(stopRequested||!task||!["running","pausing","paused","stopping"].includes(task.state))return false;stopRequested=true;pauseRequested=false;
      if(working){task.state="stopping";task.message="正在停止后续处理；已提交的本机保存需要等待结果。";notify();}else finishStopped("已停止。已经保存的资料会保留。");return true;}
    return Object.freeze({start,pause,resume,stop,reconcile,snapshot,isReconciling:()=>!disposed&&reconciling,hasReview:()=>!!task?.items.some(i=>i.state==='needs_review'),canReconcile:()=>current()&&!working&&!unsettled&&!reconciling&&typeof readHistory==='function'&&!!task?.items.some(i=>i.state==='needs_review'&&recoveries.get(i)?.serialized),hasPending:()=>working||unsettled>0,canStart:()=>current()&&!working&&!unsettled&&!task?.items.some(i=>i.state==='needs_review')&&!(task&&["running","pausing","paused","stopping"].includes(task.state)),isActive:()=>!!task&&["running","pausing","paused","stopping"].includes(task.state),
      retryFailed:()=>start((task?.items||[]).filter(item=>item.state==="failed").map(({id,name})=>({id,name}))),dispose(){disposed=true;stopRequested=true;pauseRequested=false;reconcileGeneration++;reconcileCancelled=true;recoveries.clear();}});
  }
  return Object.freeze({create,LIMIT});
});
