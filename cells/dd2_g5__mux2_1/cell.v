module dd2_g5__mux2_1 (A0, A1, S, X);
  input A0;
  input A1;
  input S;
  output X;
  assign X = (A0 & ~S) | (A1 & S);
endmodule
