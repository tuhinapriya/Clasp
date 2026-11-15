import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthorizationService } from '../src/auth/authorization.js';
import { BiometricEvidenceService, BiometricIdentityProvider } from '../src/identity/biometrics.js';
import { ProtectedPaymentTool, requestScope, type PaymentGateway } from '../src/payments/test-payment.js';

function setup() {
 const evidence = new BiometricEvidenceService();
 const auth = new AuthorizationService(new BiometricIdentityProvider(evidence), undefined, undefined, () => evidence.authorizationStatus());
 let calls = 0;
 const gateway: PaymentGateway = {create: async()=>{calls++;return {id:'pi_test',status:'succeeded',livemode:false,amount:500,currency:'usd',amount_received:500}}};
 const tool = new ProtectedPaymentTool(auth,()=>gateway);
 const record=(factor:'voice-speaker'|'polar-ppg',decision:'MATCH'|'UNCERTAIN')=>{const now=Date.now();evidence.record({factor,decision,subjectId:decision==='MATCH'?'primary-user':null,verifiedAt:new Date(now).toISOString(),expiresAt:new Date(now+15000).toISOString(),verifier:'fixture',simulated:false});};
 const run=(id:string,isStillValid=()=>true)=>requestScope.run({requestId:id,demo:true,isStillValid},()=>tool.execute({amountUsd:5,merchantId:'clasp_demo_merchant'}));
 return {evidence,auth,record,run,calls:()=>calls};
}

test('both real-factor matches allow one idempotent payment',async()=>{
 const x=setup();x.record('voice-speaker','MATCH');x.record('polar-ppg','MATCH');await x.auth.authenticate();
 const [a,b]=await Promise.all([x.run('demo-allowed-1'),x.run('demo-allowed-1')]);
 assert.equal(a.decision,'ALLOW');assert.equal(x.calls(),1);assert.deepEqual(a,b);
});
test('uncertain heartbeat blocks the real biometric gate and Stripe',async()=>{
 const x=setup();x.record('voice-speaker','MATCH');x.record('polar-ppg','UNCERTAIN');
 const a=await x.run('demo-blocked-1');assert.equal(a.decision,'DENY');assert.equal(a.stripeCalled,false);assert.equal(x.calls(),0);assert.equal(x.evidence.snapshot().polarPpg.status,'UNCERTAIN');
});
test('disconnect before execution blocks even with matching evidence',async()=>{
 const x=setup();x.record('voice-speaker','MATCH');x.record('polar-ppg','MATCH');await x.auth.authenticate();
 const a=await x.run('demo-disconnected-1',()=>false);assert.equal(a.decision,'DENY');assert.equal(x.calls(),0);
});
