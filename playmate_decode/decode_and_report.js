const RAW = require('./player_core_string_array.js');
const L = RAW.length, R = 441;
const rot = new Array(L);
for (let i = 0; i < L; i++) rot[i] = RAW[(i + R) % L];
const A = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/=';
function dec(s){let o='',t='';for(let bc=0,buf,idx,bs=0;idx=s.charAt(bs++);~idx&&(buf=bc%4?buf*64+idx:idx,bc++%4)?o+=String.fromCharCode(255&buf>>(-2*bc&6)):0){idx=A.indexOf(idx);}
for(let k=0;k<o.length;k++)t+='%'+('00'+o.charCodeAt(k).toString(16)).slice(-2);return decodeURIComponent(t);}
function S(arg){return dec(rot[arg-489]);}
// sanity anchors
console.log('ANCHOR pathname  0x521 ->', JSON.stringify(S(0x521)));
console.log('ANCHOR split     0x4ec ->', JSON.stringify(S(0x4ec)));
console.log('ANCHOR hls?      0x478 ->', JSON.stringify(S(0x478)));
console.log('');
const q = {'0x4a3 apiURL':0x4a3,'0x1ec deleted-query-param / data field':0x1ec,'0x476 api/pla+?+r':0x476,
 '0x269 Content-Type':0x269,'0x2d1 header key a':0x2d1,'0x2eb header key b':0x2eb,'0x4dd JSON.stringify':0x4dd,
 '0x20e .then':0x20e,'0x403 .json':0x403,'0x4b0 .catch':0x4b0,'0x458 beacon url':0x458,'0x204 method':0x204,
 '0x244 streaming_url':0x244,'0x2c0 title':0x2c0,'0x309 thumbnail':0x309,'0x410 playlist':0x410,
 '0x245 ad client':0x245,'0x34f ad name':0x34f,'0x39f ad name2':0x39f,'0x511 vast key':0x511,
 '0x4d8 player elem':0x4d8,'0x45a window.location':0x45a,'0x521 pathname':0x521,'0x33c URLSearchParams':0x33c,
 '0x522 searchParams.get':0x522,'0x274 delete':0x274,'0x434 toString':0x434,'0x30d getQueryParam arg':0x30d,
 '0x240 videoSubtitles a':0x240,'0x2ea videoSubtitles b':0x2ea,'0x4a6 default_sub a':0x4a6,'0x43f default_sub b':0x43f,
 '0x4d7 ang':0x4d7,'0x39d __PM.duration':0x39d,'0x506 share':0x506,'0x507 logoUrl':0x507,'0x44b logoPos':0x44b,
 '0x3e8 logoLink':0x3e8,'0x527 logo':0x527,'0x280 svg a':0x280,'0x302 join':0x302,'0x4af label':0x4af};
for (const [k,v] of Object.entries(q)) console.log(k.padEnd(38), JSON.stringify(S(v)));

console.log('\n--- beacon url fragments (flushBeacon: 0x3ad + 0x275 + 0x289) ---');
console.log(JSON.stringify(S(0x3ad)), JSON.stringify(S(0x275)), JSON.stringify(S(0x289)),
            '=> ', JSON.stringify(S(0x3ad)+S(0x275)+S(0x289)));
console.log('--- TXjrX key resolves to? ---');
console.log('0x3e9 ->', JSON.stringify(S(0x3e9)));
console.log('\n--- every rot[] entry, in order, that starts with / or contains api ---');
for (let i=0;i<L;i++){const s=dec(rot[i]); if(/^\/|api|http/i.test(s)) console.log(' raw'+i+' rot'+i+' arg=0x'+(i+489).toString(16)+' '+JSON.stringify(s));}
console.log('\n--- token at raw 289 ---', JSON.stringify(RAW[289]), 'len', RAW[289].length);
console.log('--- token at raw 103 ---', JSON.stringify(RAW[103]), 'len', RAW[103].length);

console.log('\n--- all tokens sharing the "/api/" base64 prefix, decoded ---');
RAW.forEach((t,i)=>{ if(t.startsWith('l2fWAs9')) console.log(' raw'+i+' arg=0x'+((i-R+L)%L+489).toString(16)+' tok='+t.padEnd(12)+' -> '+JSON.stringify(dec(t))); });
console.log('\n--- fragments that could extend a path: containing "/" ---');
RAW.forEach((t,i)=>{ const s=dec(t); if(s.includes('/')&&s.length<14) console.log(' raw'+i+' arg=0x'+((i-R+L)%L+489).toString(16)+' '+JSON.stringify(s)); });
