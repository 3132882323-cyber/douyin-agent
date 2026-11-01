/* Explicit, page-bound product reads and local saves. No platform writes. */
(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;else root.DianProductPackageQueue=api;})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  const LIMIT=100;
  function ownValue(value,key){try{return value&&(typeof value==="object"||typeof value==="function")?Object.getOwnPropertyDescriptor(value,key)?.value:undefined;}catch{return undefined;}}
  const preflightMessages=new Set(["保存尚未开始。当前商品资料无法核对，请重新读取商品后再保存。","保存尚未开始。页面或商品资料已变化，请重新读取商品后再保存。","保存尚未开始。资料库读取超时，请等待旧请求结束后重试。","保存尚未开始。扩展连接不可用，请保留当前编辑，刷新抖店页面后重试。","保存尚未开始。暂时无法读取或核对本机资料库，请重新打开资料库检查后重试。"]);
  function create({readModel,save,readHistory,isCurrent,onChange=()=>{},readTimeoutMs=30000,saveTimeoutMs=30000,reconcileTimeoutMs=30000,interItemDelayMs=0,clock,random=Math.random}){
    if([readModel,save,isCurrent].some(fn=>typeof fn!=="function"))throw Error("商品读取组件未就绪。");
    if([readTimeoutMs,saveTimeoutMs,reconcileTimeoutMs].some(n=>!Number.isSafeInteger(n)||n<1||n>120000)||readHistory!==undefined&&typeof readHistory!=="function")throw Error("商品处理超时设置或只读核对配置无效。");
    const delay=typeof interItemDelayMs==='number'?{min:interItemDelayMs,max:interItemDelayMs}:interItemDelayMs;
    if(!delay||[delay.min,delay.max].some(n=>!Number.isSafeInteger(n)||n<0||n>120000)||delay.min>delay.max||typeof random!=='function'||clock!==undefined&&(!clock||['now','setTimeout','clearTimeout'].some(k=>typeof clock[k]!=='function')))throw Error('逐件间隔设置无效，请使用 0 至 120 秒范围。');
    const delayRange={min:delay.min,max:delay.max},now=clock?()=>clock.now():()=>globalThis.performance?.now?.()??Date.now(),schedule=clock?(fn,ms)=>clock.setTimeout(fn,ms):(fn,ms)=>setTimeout(fn,ms),unschedule=clock?id=>clock.clearTimeout(id):id=>clearTimeout(id);
    let task=null,working=false,disposed=false,pauseRequested=false,stopRequested=false,consecutiveFailures=0;
    let unsettled=0,readSettlementUnknown=false;
    let nextRunAt=0,delayDuration=0,lastItemAt=null,cancelWait=null;
    const unknownReadMessage="上一读取的结束状态无法确认，请刷新抖店页面后重新选择商品。当前批次不会继续保存。";
    const recoveries=new Map();let reconciling=false,reconcileCancelled=false,reconcileGeneration=0;
    function safeCopy(value,depth=0,budget={nodes:0}){if(depth>20||++budget.nodes>100000)throw Error('资料超过本机核对结构上限。');if(value===null||typeof value==='boolean')return value;if(typeof value==='string'){if(value.length>12000)throw Error('资料文本超过核对上限。');return value;}if(typeof value==='number'&&Number.isFinite(value)&&Math.abs(value)<=Number.MAX_SAFE_INTEGER)return value;if(Array.isArray(value)){if(value.length>5000||Reflect.ownKeys(value).length!==value.length+1)throw Error('资料数组无效。');return Array.from({length:value.length},(_,i)=>{const d=Object.getOwnPropertyDescriptor(value,String(i));if(!d||!Object.hasOwn(d,'value'))throw Error('资料访问器或缺项不能核对。');return safeCopy(d.value,depth+1,budget);});}if(value&&typeof value==='object'&&[Object.prototype,null].includes(Object.getPrototypeOf(value))){const out=Object.create(null);for(const key of Reflect.ownKeys(value).sort()){const d=Object.getOwnPropertyDescriptor(value,key);if(typeof key!=='string'||key.length>128||!Object.hasOwn(d,'value'))throw Error('资料访问器不能核对。');out[key]=safeCopy(d.value,depth+1,budget);}return out;}throw Error('资料不是完整可核对的数据。');}
    function expectedProduct(value,id){const product=safeCopy(value),s=product.source;if(!s||s.platform!=='doudian'||s.product_id!==id||!['doudian_store_product','doudian_edit_product','doudian_draft_product'].includes(s.source_kind)||!/^archive_[a-f0-9]{64}$/.test(s.store_key||'')||typeof s.context_key!=='string'||!s.context_key||!Number.isFinite(Date.parse(s.captured_at)))throw Error('商品本店来源或版本范围不足，不能自动核对未知保存。');const serialized=JSON.stringify(product);if(new TextEncoder().encode(serialized).length>4*1024*1024)throw Error('完整商品超过本次核对4MiB上限，未截断。');return{serialized,product_id:id,source_kind:s.source_kind,store_key:s.store_key,captured_at:s.captured_at,group_key:JSON.stringify([s.source_kind,id,s.store_key,''])};}
    function historyMatches(history,expected){const h=safeCopy(history);if(h.historical!==true||h.execution_authorized!==false||!Number.isSafeInteger(h.revision)||h.revision<0||!Array.isArray(h.groups)||h.groups.length>100)throw Error('资料库只读回执格式未通过。');const keys=new Set();let found=false;for(const group of h.groups){if(typeof group.key!=='string'||keys.has(group.key)||typeof group.anonymous_slot!=='string'||!Array.isArray(group.snapshots)||!group.snapshots.length||group.snapshots.length>8)throw Error('资料库分组或版本无效。');keys.add(group.key);let previous=Infinity;for(const row of group.snapshots){const p=row.product,s=p?.source,saved=Date.parse(row.saved_at),captured=Date.parse(s?.captured_at);if(!s||!Number.isFinite(saved)||saved>previous||!Number.isFinite(captured)||captured>saved+60000||group.key!==JSON.stringify([s.source_kind,s.product_id,s.store_key||'',s.store_key?'':group.anonymous_slot])||s.store_key&&group.anonymous_slot!==''||!s.store_key&&!/^anonymous-\d+$/.test(group.anonymous_slot))throw Error('资料库版本来源未吻合。');previous=saved;if(group.key===expected.group_key&&s.product_id===expected.product_id&&s.source_kind===expected.source_kind&&s.store_key===expected.store_key&&s.captured_at===expected.captured_at&&JSON.stringify(p)===expected.serialized)found=true;}}return found;}
    async function bounded(operation,ms,code,onTimeout=()=>{}){
      let timer;unsettled++;
      let pending;try{pending=Promise.resolve(operation());}catch(error){pending=Promise.reject(error);}
      const release=()=>{unsettled--;notify();};
      pending.then(release,error=>{
        // A Library timeout settles its wrapper, not the underlying runtime request.
        // Only accept a native Promise signal, without executing adapter accessors.
        if(error&&(typeof error==="object"||typeof error==="function")){
          if(Object.getOwnPropertyDescriptor(error,"transportSettlementUnknown")?.value===true)readSettlementUnknown=true;
          const signal=Object.getOwnPropertyDescriptor(error,"transportSettled")?.value;
          if(signal)try{Promise.prototype.then.call(signal,confirmed=>{if(confirmed===false){readSettlementUnknown=true;if(task?.state==="stopped")task.message=unknownReadMessage;}release();},release);return;}catch{/* Non-Promise metadata cannot release or hold the queue. */}
          if(ownValue(error,"code")==="MODEL_READ_PENDING")readSettlementUnknown=true;
        }
        release();
      });
      try{return await Promise.race([pending,new Promise((_,reject)=>{timer=schedule(()=>{onTimeout();const error=Error(code==="PACKAGE_READ_TIMEOUT"?"商品读取超时，已停止后续；晚到资料不会保存。":code==="PACKAGE_RECONCILE_TIMEOUT"?"资料库核对超时，旧结果不会采用；没有重复保存。":"本机保存超时，结果待核对；不会自动重复保存。");error.code=code;reject(error);},ms);})]);}
      finally{unschedule(timer);}
    }
    function snapshot(){return task?JSON.parse(JSON.stringify(task)):null;}
    function notify(){if(!disposed)try{onChange(snapshot());}catch{/* UI failure cannot replay a committed save. */}}
    function current(){try{return !disposed&&isCurrent()===true;}catch{return false;}}
    function chooseDelay(){let value=0;try{value=random();}catch{}if(!Number.isFinite(value)||value<0||value>=1)value=0;return delayRange.min+Math.floor(value*(delayRange.max-delayRange.min+1));}
    function setNextRun(){lastItemAt=now();delayDuration=chooseDelay();nextRunAt=lastItemAt+delayDuration;}
    // A cancellable gap is not a transport request and has no read/save timeout.
    // Pausing cancels only the timer; elapsed wall time still counts on resume.
    function waitForNext(){
      if(nextRunAt<=now())return Promise.resolve();
      return new Promise(resolve=>{let timer,done=false,shownSeconds=null;const finish=()=>{if(done)return;done=true;unschedule(timer);cancelWait=null;task.delay=null;resolve();};cancelWait=finish;
        const tick=()=>{if(done)return;if(stopRequested||pauseRequested||!current()){finish();return;}const remaining=Math.max(0,nextRunAt-now());if(!remaining){finish();return;}const seconds=Math.ceil(remaining/1000);task.delay={remaining_ms:remaining,duration_ms:delayDuration,next_id:task.items[task.next].id};if(shownSeconds!==seconds){shownSeconds=seconds;task.message='间隔等待：约 '+seconds+' 秒后读取下一件商品，可立即暂停或停止。';notify();}if(done)return;timer=schedule(tick,Math.min(250,remaining));};tick();
      });
    }
    function finishStopped(message){task.state="stopped";task.delay=null;task.message=message;for(const item of task.items)if(item.state==="waiting")item.state="not_started";notify();}
    async function run(){
      if(working||disposed||!task)return snapshot();
      working=true;task.state="running";task.message="正在读取并保存…";notify();
      try{
        while(task.next<task.items.length){
          if(stopRequested||!current()){finishStopped(stopRequested?"已停止。已经保存的资料会保留。":"页面或店铺已变化，已停止剩余商品。");break;}
          if(pauseRequested){task.state="paused";task.message="已暂停，可继续剩余商品。";notify();break;}
          if(nextRunAt>now())await waitForNext();
          if(stopRequested||!current()){finishStopped(stopRequested?"已停止。已经保存的资料会保留。":"页面或店铺已变化，已停止剩余商品。");break;}
          if(pauseRequested){task.state="paused";task.message="已暂停，下一件尚未开始读取。继续后仍按所选间隔处理。";notify();break;}
          const item=task.items[task.next];item.state="reading";item.message="正在读取商品资料";notify();
          if(stopRequested||!current()){item.state="not_started";item.message="读取开始前已停止，没有请求商品资料。";task.next++;finishStopped("页面已变化或任务已停止，剩余商品未处理。");break;}
          let product;
          try{const response=await bounded(()=>readModel(item.id),readTimeoutMs,"PACKAGE_READ_TIMEOUT");product=safeCopy(response?.product);if(!product||product.source?.product_id!==item.id)throw Error("读取结果与所选商品不一致。");}
          catch(error){
            setNextRun();
            if(stopRequested||!current()){item.state="not_saved";item.message="读取已停止，没有开始保存。";task.next++;finishStopped("页面已变化或任务已停止，剩余商品未处理。");break;}
            const code=ownValue(error,"code");
            item.state="failed";item.message=readSettlementUnknown?unknownReadMessage:code==="PAGE_CHANGED"?"读取期间页面已变化，未采用旧商品资料，请重新读取。":["LOGIN_OR_PERMISSION","MODEL_SENDER_REJECTED"].includes(code)?"当前页面或读取权限已变化，请检查抖店页面。":code==="MODEL_READ_PENDING"?"上一条商品读取尚未结束，请等待旧请求结束后重试。":["PACKAGE_READ_TIMEOUT","READ_TIMEOUT"].includes(code)?"商品读取超时，已停止后续；旧请求结束前不能重试，晚到资料不会保存。":"商品资料读取或核对未完成，请检查当前抖店页面后重试。";task.next++;consecutiveFailures++;
            if(readSettlementUnknown){finishStopped(unknownReadMessage);break;}
            if(["LOGIN_OR_PERMISSION","PAGE_CHANGED","MODEL_SENDER_REJECTED"].includes(code)){finishStopped("当前页面或读取权限已变化，请检查抖店页面。");break;}
            if(["PACKAGE_READ_TIMEOUT","READ_TIMEOUT","MODEL_READ_PENDING"].includes(code)||unsettled){finishStopped(code==="MODEL_READ_PENDING"?"上一条商品读取尚未结束，已停止本批；剩余商品未开始，旧请求结束后可重新选择。":"读取超时，已停止本批；旧请求结束前不会启动另一批，晚到资料不会采用。");break;}
            if(consecutiveFailures>=3){task.state="paused";pauseRequested=true;task.message="连续 3 件读取失败，已暂停。检查页面后可继续。";notify();break;}
            notify();continue;
          }
          if(stopRequested||!current()){item.state="not_saved";item.message="读取已结束，未开始保存。";task.next++;setNextRun();finishStopped("已停止。当前未保存项不会继续写入资料库。");break;}
          item.state="saving";item.message="正在保存到本机";notify();
          if(stopRequested||!current()){item.state="not_saved";item.message="保存开始前已停止，没有请求写入资料库。";task.next++;setNextRun();finishStopped("当前商品未开始保存，已停止后续处理。");break;}
          let saveLive=true,expected=null,recoveryReason='';try{expected=expectedProduct(product,item.id);}catch(e){recoveryReason=e.message;}
          try{
            const receipt=await bounded(()=>save(product,()=>saveLive&&current()&&!stopRequested),saveTimeoutMs,"PACKAGE_SAVE_TIMEOUT",()=>{saveLive=false;});
            saveLive=false;
            if(receipt?.historical!==true||receipt.execution_authorized!==false)throw Error("保存结果尚未确认。");
            item.state="saved";item.message="已保存到资料库";consecutiveFailures=0;task.next++;setNextRun();notify();
          }catch(error){
            setNextRun();
            const code=ownValue(error,"code");
            if(code==="PACKAGE_SAVE_NOT_STARTED"&&ownValue(error,"saveNotStarted")===true){
              const stopped=stopRequested||!current(),message=ownValue(error,"message");item.state=stopped?"not_saved":"failed";item.failure_stage="before_save_request";item.message=preflightMessages.has(message)?message:unsettled?"保存尚未开始。请等待旧请求结束后重试。":"保存尚未开始。请重新打开资料库检查后重试。";task.next++;
              finishStopped(stopped?"当前商品没有开始保存，已停止后续处理。":unsettled?"保存前的资料库读取尚未结束，本批已停止。结束后可重试，当前商品未写入资料库。":"保存尚未开始，已停止本批；请检查资料库后重试。");break;
            }
            if(expected)recoveries.set(item,expected);else recoveries.set(item,{unavailable:recoveryReason});
            item.state="needs_review";item.message=(["PACKAGE_SAVE_TIMEOUT","PACKAGE_LIBRARY_TIMEOUT"].includes(code)?"本机保存超时，结果待核对；旧请求结束前不能核对或重试。":"保存结果待核对，请查看资料库；不会自动重复保存。")+(expected?'':' 缺少完整的商品来源记录，仍需人工核对。');task.next++;
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
      if(readSettlementUnknown)throw Error(unknownReadMessage);
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
    function pause(){if(!working||!task||stopRequested||pauseRequested)return false;pauseRequested=true;const waiting=!!cancelWait;task.state="pausing";task.message=waiting?"正在暂停，下一件尚未开始读取。":"当前商品处理完后暂停。";cancelWait?.();notify();return true;}
    async function resume(){if(disposed||working||unsettled||readSettlementUnknown||task?.state!=="paused")return snapshot();if(!current()){finishStopped("页面或店铺已变化，请重新选择商品。");return snapshot();}pauseRequested=false;consecutiveFailures=0;return run();}
    async function reconcile({confirmed=false}={}){
      if(confirmed!==true)throw Error('请明确确认仅只读核对资料库，不会重复保存。');
      if(disposed||working||unsettled||reconciling||!current())throw Error('旧请求尚未结束或来源已变化，不能核对。');
      if(typeof readHistory!=='function')throw Error('资料库只读核对连接尚未接入。');
      const ownerTask=task,items=task?.items.filter(i=>i.state==='needs_review')||[];
      if(!items.length)throw Error('没有待核对保存。');
      if(!items.some(i=>recoveries.get(i)?.serialized))throw Error('没有完整本店私有版本依据，不能自动核对；请查看资料库。');
      const rev=++reconcileGeneration;reconciling=true;reconcileCancelled=false;working=true;task.message='正在只读核对资料库；没有再次保存。';notify();
      try{
        if(!current()||reconcileCancelled)throw Error('核对已停止或来源变化。');
        const history=await bounded(()=>readHistory(),reconcileTimeoutMs,'PACKAGE_RECONCILE_TIMEOUT',()=>{reconcileCancelled=true;});
        if(disposed||task!==ownerTask||rev!==reconcileGeneration)return snapshot();
        if(!current()||reconcileCancelled)throw Error('范围已变化或核对已停止，未采用回执。');
        const results=items.map(item=>{
          const expected=recoveries.get(item);
          if(!expected||expected.unavailable)return{item,found:false,message:'缺少完整的商品来源记录，仍需人工核对。'};
          const found=historyMatches(history,expected);
          return{item,found,message:found?'资料库当前存在完全相同版本；不能归因本次保存请求。':'本次读取未找到完全相同版本，仍待核对；不会重新保存。'};
        });
        if(!current()||reconcileCancelled||disposed||task!==ownerTask)throw Error('核对来源已变化。');
        for(const r of results){r.item.message=r.message;if(r.found){r.item.state='saved';r.item.recovery='current_version_present_not_io_attributed';recoveries.delete(r.item);}}
        task.message='只读核对结束：当前存在不等于本次IO归因；未匹配项仍待核对。';notify();
      }catch(error){
        if(!disposed&&task===ownerTask&&rev===reconcileGeneration){
          const code=ownValue(error,"code");
          task.message=!current()?"核对期间页面或店铺已变化，结果未采用；请重新核对资料库。":["PACKAGE_RECONCILE_TIMEOUT","PACKAGE_LIBRARY_TIMEOUT"].includes(code)?"资料库核对超时，旧请求结束前不能再次核对；不会重复保存。":reconcileCancelled?"核对已停止，结果未采用；没有再次保存。":"资料库读取或核对未完成，仍待核对；没有再次保存。";
          notify();
        }
      }finally{reconciling=false;working=false;notify();}
      return snapshot();
    }
    function stop(){if(reconciling){if(reconcileCancelled)return false;reconcileCancelled=true;task.message='已停止采用核对结果；底层只读请求仍可能未结束。';notify();return true;}if(stopRequested||!task||!["running","pausing","paused","stopping"].includes(task.state))return false;stopRequested=true;pauseRequested=false;
      if(working){const waiting=!!cancelWait;task.state="stopping";task.message=waiting?"已停止等待，下一件不会开始读取。":"正在停止后续处理；已提交的本机保存需要等待结果。";cancelWait?.();notify();}else finishStopped("已停止。已经保存的资料会保留。");return true;}
    function setInterItemDelayMs(value){const range=typeof value==='number'?{min:value,max:value}:value;if(!range||[range.min,range.max].some(n=>!Number.isSafeInteger(n)||n<0||n>120000)||range.min>range.max)throw Error('逐件间隔设置无效，请使用 0 至 120 秒范围。');if(disposed||working||unsettled||readSettlementUnknown||reconciling||task&&['running','pausing','paused','stopping'].includes(task.state)||task?.items.some(i=>i.state==='needs_review'))throw Error('请先停止当前批次并核对保存结果，再调整逐件间隔。');delayRange.min=range.min;delayRange.max=range.max;delayDuration=chooseDelay();nextRunAt=lastItemAt===null?0:lastItemAt+delayDuration;return true;}
    return Object.freeze({start,pause,resume,stop,reconcile,snapshot,isReconciling:()=>!disposed&&reconciling,isReadSettlementUnknown:()=>readSettlementUnknown,hasReview:()=>!!task?.items.some(i=>i.state==='needs_review'),canReconcile:()=>current()&&!working&&!unsettled&&!readSettlementUnknown&&!reconciling&&typeof readHistory==='function'&&!!task?.items.some(i=>i.state==='needs_review'&&recoveries.get(i)?.serialized),hasPending:()=>working||unsettled>0||readSettlementUnknown,canStart:()=>current()&&!working&&!unsettled&&!readSettlementUnknown&&!task?.items.some(i=>i.state==='needs_review')&&!(task&&["running","pausing","paused","stopping"].includes(task.state)),isActive:()=>!!task&&["running","pausing","paused","stopping"].includes(task.state),
      setInterItemDelayMs,isWaiting:()=>!!cancelWait,hasInflight:()=>unsettled>0,retryFailed:()=>start((task?.items||[]).filter(item=>item.state==="failed").map(({id,name})=>({id,name}))),dispose(){disposed=true;stopRequested=true;pauseRequested=false;cancelWait?.();reconcileGeneration++;reconcileCancelled=true;recoveries.clear();}});
  }
  return Object.freeze({create,LIMIT});
});
