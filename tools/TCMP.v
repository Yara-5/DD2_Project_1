/*

    Copyright 2016, mshalan@aucegypt.edu

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
*/

/*  
    A serial-parallel multiplier modeled after: https://t.ly/inylp
*/

`timescale 1ns/1ps
`default_nettype none

module TCMP(clk, rst, a, s);
    input clk, rst;
    input a;
    output reg s;

    reg z;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            s <= 1'b0;
            z <= 1'b0;
        end
        else begin
            z <= a | z;
            s <= a ^ z;
        end
    end

endmodule

